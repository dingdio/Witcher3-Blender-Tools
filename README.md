# Witcher 3 Blender Tools
Blender addon for working with Witcher 3 files with some support for Witcher 2 files. Get the latest [Release](https://github.com/dingdio/Witcher3-Blender-Tools/releases)

<img src="https://user-images.githubusercontent.com/4729750/227740200-4722f6c0-fce9-43f5-a4c2-68d6b90c706a.jpg" height="200">

## Features

### Witcher 3
- Mesh importer/export (.w2mesh)
- Rig import/export (.w2rig, .json)
- Animation import/export (.w2anims, .json)
- Map importer (.w2l, .w2w)
- Basic map layer definition export for [radish tools](https://www.nexusmods.com/witcher3/mods/3620) (.yml)
- Characters/Entity definition (.w2ent) importer
- WIP Scene/Cutscene import (.w2scene, .w2cutscene)
- Lipsync Animation (.cr2w)

### Witcher 2 / REDkit
- .w2mesh import
- .w2rig import
- .w2l import
- Works best with [REDkit](https://redkitwiki.cdprojektred.com/welcome+to+the+redkit+wiki.htm) files.

Check out the [wiki](https://github.com/dingdio/Witcher3-Blender-Tools/wiki) for info on usage.

## Requirements
- Blender ~3.6

- [ArdCarraigh Blender_APX_Addon](https://github.com/ArdCarraigh/Blender_APX_Addon) - The APX addon is needed to load any redcloth items. You will have to export or download all apx from the game and add the Redcloth repo in addon settings. [Download all vanilla apx here.](https://mega.nz/file/CchGVCSb#ahDgIhxvicClEA9VHecPu6S95iT8ih2Q5kRMUHoY5ec)

    ### **Animation**:
    The current best way to work with the exported animation json is to use this specific version of Wolvenkit 0.6.1 compiled by nikich340 until Wolvenkit-7 is updated. Also the utility tool for modifying the exported files.
- [0.6.1-YML-W2ANIMS-APRIL04](https://github.com/nikich340/WolvenKit/releases/tag/0.6.1-YML-W2ANIMS-APRIL04)
- [W3-Maya-AnimUtil](https://github.com/nikich340/W3-Maya-AnimUtil) 
- [Some Wiki Notes](https://github.com/dingdio/Witcher3_Blender_Tools/wiki/Exporting-Animation-Notes)

## Recommended 

- [BlenderNormalGroups](https://github.com/theoldben/BlenderNormalGroups) Can switch normal map nodes to a faster custom node for better animation playback.

- [Prolog World Terrain Setup](https://mega.nz/file/WNZzCQQR#KICtWteq_OxwU_YKj4LU09kdJlBMqzzwIJd8DVGil4Q) - Not really required but has an example of how I set up basic terrain for the prolog world. It won't match how it looks in game but good enough to place game objects in blender. video - https://www.youtube.com/watch?v=qlRfUGMCyvQ

## Installation
Put "io_import_w2l" folder into your blender addons folder
Launch Blender and activate the addon in Blender Preferences

#### Settings for Witcher 3
in the addon settings add your own paths to:
- uncook_path = main repo where you exported all the game bundles with wcclite.exe .w2mesh files, .w2mi files, .w2ent etc.
- tex_uncook_path = repo folder with ALL exported .tga/.png/.dds from the game
- apx_uncook_path = repo folder with ALL exported .apx from the game

#### Settings for Witcher 2 / REDKit
- Witcher 2 Path = This should be the path to your Wither 2 Instalation with REDKit also installed.

### WolvenKit 7 integration
- WolvenKit 7 CLI = path to WolvenKit.CLI.exe
- Your Wolvenkit Project Path = this is the default path meshes will export to. The mesh importer will also check this project first for raw .tga files when loading a new w2mesh

## Optional Setup

### Speech / Lipsync repo settings
- Extracted Lipsync repo = folder with .cr2w files
- Converted wem files = folder with (.ogg)s 
- [Wiki about speech](https://github.com/dingdio/Witcher3-Blender-Tools/wiki/Speech-and-Lipsync-Notes)

### FBX repo settings
>*The tools now import and export w2mesh directly it is not recommended to use FBX*
- [FBX Import plugin for blender](https://www.nexusmods.com/witcher3/mods/6118) - For working with any fbx files.
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
