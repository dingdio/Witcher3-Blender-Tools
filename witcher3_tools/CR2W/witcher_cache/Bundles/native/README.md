# Native Doboz decompressor

Doboz is one of the four compression formats used by Witcher 3 `.bundle` files
(around 2.6% of entries; the rest are uncompressed, Zlib, LZ4 or Snappy). It
has no Python implementation here, so it is loaded as a native library through
`ctypes` by `../BundleItem.py`, which picks the file matching the host:

| Platform | Library | Shipped |
|---|---|---|
| Windows | `Doboz.dll` | Yes |
| Linux | `Doboz.so` | Yes |
| macOS | `Doboz.dylib` | No — build it yourself, see below |

The library is loaded lazily on first use rather than at import time, so a
missing or unloadable build does not break addon registration. Instead the
affected extraction raises `MissingCompressionException` naming Doboz, and
every other compression format keeps working.

## Upstream

Doboz Data Compression Library, Copyright (C) 2010-2011 Attila T. Áfra,
distributed under the zlib license. The Linux build here was compiled from the
mirror at <https://github.com/nemequ/doboz>.

Only the decompressor is needed at runtime — `Decompressor.cpp` and its
headers. The compressor is not used and does not need to be linked in.

## Exported ABI

The addon calls a single C symbol, matching the contract of the original
Windows DLL:

```c
int Decompress(const void* source, size_t sourceSize,
               void* destination, size_t destinationSize);
```

It returns `0` (`doboz::RESULT_OK`) on success.

## Rebuilding

Upstream Doboz builds a C++ library, so the one piece that has to be added is a
thin `extern "C"` wrapper exposing the symbol above:

```cpp
// doboz_capi.cpp — place next to the Doboz sources
#include "Decompressor.h"

extern "C" int Decompress(const void* source, size_t sourceSize,
                          void* destination, size_t destinationSize)
{
    doboz::Decompressor decompressor;
    return static_cast<int>(
        decompressor.decompress(source, sourceSize, destination, destinationSize));
}
```

Then, from the directory holding the Doboz sources:

```sh
g++ -O2 -fPIC -shared -DNDEBUG -include cstddef \
    -o Doboz.so doboz_capi.cpp Decompressor.cpp -I.
```

For macOS, the same command with `-o Doboz.dylib` produces the file
`BundleItem.py` looks for.

`-include cstddef` is required because the upstream sources use `size_t`
without including `<cstddef>`, relying on it arriving transitively through
other headers — which newer libstdc++ versions no longer guarantee.

Verify a fresh build round-trips before shipping it: compress a buffer with
the upstream `Compressor`, decompress it through the built library, and check
the result is byte-identical to the input.
