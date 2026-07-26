// C ABI wrapper around doboz::Decompressor, matching the signature the
// Witcher3-Blender-Tools addon expects from Doboz.dll:
//   int Decompress(const void* source, size_t sourceSize, void* destination, size_t destinationSize)
// Returns 0 (doboz::RESULT_OK) on success, matching the original Windows DLL's contract.
#include "Decompressor.h"

extern "C" int Decompress(const void* source, size_t sourceSize, void* destination, size_t destinationSize)
{
    doboz::Decompressor decompressor;
    return static_cast<int>(decompressor.decompress(source, sourceSize, destination, destinationSize));
}
