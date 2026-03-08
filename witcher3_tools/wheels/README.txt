Wheels for compiled Python deps (Windows, Blender 4.5 / CP311).

To add compiled deps:
1) Drop the wheel file in this folder.
2) Add the filename to `witcher3_tools/blender_manifest.toml` under `wheels`.

Notes:
- Wheels must be unmodified.
- Blender installs these into the extension's site-packages at enable time.
