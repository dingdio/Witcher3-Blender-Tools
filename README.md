# Witcher 3 Blender Tools
Blender addon for working with Witcher 3 files. Very much a work in progress. Many things not tested or fully implemented. Buttons like "export .w2anims" not implemented yet.

## Features

- w2l and w2w importer
- character w2ent importer
- w2rig importer
- animation importer
- lipsync importer

## NOT IMPLEMENTED


- exporting rigs and anims
- ui for exporting w2l. There is file called TEMP_EXPORT_RADISH.py that has an example of how it might work that you can run in Blender manually.


## Requirements
- Blender ~3.2.1

- [FBX Import plugin for blender](https://www.nexusmods.com/witcher3/mods/6118) - Won't work without this.

- [yaml for python](https://mega.nz/file/PJJARA5S#jDFjV18W6JCB-NAs_NPm8iVAseMmGkL7IH7t5fw_HTU) - Put this yaml folder in your blender addons folder if you're getting a yaml error. [yaml on git](https://github.com/yaml/pyyaml/tree/master/lib/yaml)

- [ArdCarraigh Blender_APX_Addon](https://github.com/ArdCarraigh/Blender_APX_Addon) - The APX addon is needed to load any redcloth items. You will have to export all apx from the game and add the Redcloth repo in addon settings

- [Prolog World Terrain Setup](https://mega.nz/file/WNZzCQQR#KICtWteq_OxwU_YKj4LU09kdJlBMqzzwIJd8DVGil4Q) - Not really required but has an example of how I set up basic terrain for the prolog world. It won't match how it looks in game but good enough to place game objects in blender. video - https://www.youtube.com/watch?v=qlRfUGMCyvQ

## Installation
Put "io_import_w2l" folder into your blender addons folder
Launch Blender, active the addon in Blender Preferences

in the addon settings add your own paths to:
- uncook_path = main repo where you exported all the game bundles with wcclite.exe
- fbx_uncook_path = repo folder with ALL exported .fbx from the game
- tex_uncook_path = repo folder with ALL exported .tga from the game

All these folders can be the same repo


