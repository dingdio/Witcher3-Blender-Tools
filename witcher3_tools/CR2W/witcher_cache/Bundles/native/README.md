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

## Why build from source instead of shipping a wheel?

The addon already ships a native dependency as a wheel — `cramjam`, for LZ4 and
Snappy — so the obvious question is why Doboz is not handled the same way.

On packaging mechanics alone, a wheel would be the better answer. It is the
*only* fully platform-aware mechanism in Blender's extension system: the client
installer selects wheels by platform tag automatically, and
`--split-platforms` filters the `wheels` list and nothing else, so raw
`.so`/`.dll` files are copied into every platform archive unchanged.

What rules it out is the verification rule. extensions.blender.org checks every
bundled wheel's SHA-256 against `https://pypi.org/pypi/{name}/{version}/json`
and flags any mismatch — the server-side enforcement of "wheels must be bundled
unmodified from Python's package index." `cramjam` passes that trivially
because it is a real published PyPI package. Doboz is not on PyPI at all, so
shipping it as a wheel would mean publishing and maintaining a PyPI package
purely to deliver a 15 KB shared library. A wheel is also still a binary
artifact in the diff, which is what vendoring source avoids.

So the two dependencies differ in kind rather than being treated
inconsistently:

| | Doboz | cramjam |
|---|---|---|
| Language | ~350 lines of dependency-free C++ | Rust, with a toolchain you cannot assume |
| On PyPI | No | Yes |
| Approach | vendor source, build on demand | vendor the published wheel |

The remaining asymmetry is real and worth stating: Doboz needs a C++ compiler
at runtime, which the cramjam wheel does not. That is the accepted tradeoff —
it is a one-off sub-second build, it is lazy so it never blocks addon
registration, and if no compiler exists only Doboz entries are affected.

## Verifying a build

Round-trip a buffer: compress it with the upstream `Compressor` and check that
decompressing through the built library reproduces the input byte for byte.
Comparing output against a known-good build across a range of entry sizes
(including the ~7.7 MB largest Doboz entry in a GOTY install) is a good check
that a toolchain change has not altered behaviour.
