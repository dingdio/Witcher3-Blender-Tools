Wheels for compiled Python deps (Blender 4.5 bundles CPython 3.11).

Blender selects the matching wheel per platform from the `wheels` list in
blender_manifest.toml, so wheels for several platforms can be listed at once.
Matching is on the wheel's python/abi/platform tags. This is the only
platform-aware packaging mechanism Blender's extension system has -- notably,
`blender --command extension build --split-platforms` filters this list and
nothing else, so raw .dll/.so files are copied into every platform archive
unchanged.

To add compiled deps:
1) Download the wheel with pip, e.g.
     pip download <pkg>==<ver> --dest . --only-binary=:all: --no-deps \
       --python-version=3.11 --platform=manylinux_2_17_x86_64
2) Add the filename to `witcher3_tools/blender_manifest.toml` under `wheels`.

Notes:
- Wheels must be unmodified from PyPI. extensions.blender.org verifies each
  wheel's sha256 against the PyPI JSON API and flags any mismatch, so a
  locally-built or repacked wheel will not pass review. This also means a
  dependency that is not already published on PyPI cannot be shipped this way
  without publishing and maintaining a package for it.
- Blender installs these into the extension's site-packages at enable time.

Currently listed:
- cramjam (LZ4 + Snappy bundle decompression), win_amd64 and manylinux x86_64.
  No macOS wheel is listed yet, so LZ4/Snappy are unavailable there.

Not shipped as a wheel: the Doboz decompressor, which is built from vendored
source instead. See ../CR2W/witcher_cache/Bundles/native/README.md for why the
two are handled differently.
