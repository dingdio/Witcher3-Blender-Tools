# Native Doboz decompressor

Doboz is one of the four compression formats used by Witcher 3 `.bundle` files
(around 2.6% of entries; the rest are uncompressed, Zlib, LZ4 or Snappy). It
has no Python implementation here, so it is used as a native library through
`ctypes` from `../BundleItem.py`.

Rather than committing a binary for every platform, only the Windows build is
vendored. Everywhere else the library is compiled from the sources in `src/`
the first time a Doboz-compressed entry is read, and cached per user.

## Resolution order

`get_doboz_lib()` tries, in order:

1. **A prebuilt library next to this file** — `Doboz.dll` on Windows,
   `Doboz.so` / `Doboz.dylib` elsewhere if you choose to drop one in.
2. **A previously compiled copy** in the per-user cache.
3. **A fresh build** from `src/`, written to that cache.

If all three fail it raises `MissingCompressionException("Doboz", ...)` listing
what was tried. Every other compression format keeps working — only Doboz
entries are affected. The library is resolved lazily on first use, so a machine
with no compiler still registers and runs the addon normally.

The cache directory is Blender's per-user extension path
(`bpy.utils.extension_path_user`), falling back to `$XDG_CACHE_HOME/witcher3_tools`
when CR2W is used outside Blender. The extension's own directory is never
written to, since it may be read-only.

## Building

Needs any C++ compiler on `PATH` — `g++`, `clang++` or `c++`. There is no
configure step and no dependencies beyond the C++ standard library. Building
takes well under a second.

The addon does this itself, but the equivalent by hand is:

```sh
c++ -O2 -fPIC -shared -DNDEBUG -include cstddef -I src \
    -o Doboz.so src/doboz_capi.cpp src/Decompressor.cpp
```

Use `-o Doboz.dylib` on macOS. `-include cstddef` is required because the
upstream sources use `size_t` without including `<cstddef>`, relying on it
arriving transitively through other headers — which newer libstdc++ versions no
longer guarantee.

Windows has no compiler by default, which is why `Doboz.dll` stays vendored.

## `src/`

| File | Origin |
|---|---|
| `Decompressor.cpp`, `Decompressor.h`, `Common.h` | Doboz, unmodified |
| `COPYING.txt` | Doboz license |
| `doboz_capi.cpp` | This project — C ABI wrapper |

Doboz Data Compression Library, Copyright (C) 2010-2011 Attila T. Áfra,
distributed under the zlib license, taken from the mirror at
<https://github.com/nemequ/doboz>. Only the decompressor is needed at runtime;
`Compressor.cpp` and `Dictionary.cpp` are not included.

`doboz_capi.cpp` exists because upstream builds a C++ library, while `ctypes`
needs a plain C symbol. It exposes exactly one function, matching the contract
of the original Windows DLL:

```c
int Decompress(const void* source, size_t sourceSize,
               void* destination, size_t destinationSize);
```

It returns `0` (`doboz::RESULT_OK`) on success.

## Verifying a build

Round-trip a buffer: compress it with the upstream `Compressor` and check that
decompressing through the built library reproduces the input byte for byte.
Comparing output against a known-good build across a range of entry sizes
(including the ~7.7 MB largest Doboz entry in a GOTY install) is a good check
that a toolchain change has not altered behaviour.
