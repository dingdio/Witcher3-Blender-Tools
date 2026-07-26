import logging
import mmap
import os
import tempfile
import shutil

log = logging.getLogger(__name__)

cramjam_lz4 = None
cramjam_snappy = None
try:
    from cramjam import lz4 as _cramjam_lz4
    from cramjam import snappy as _cramjam_snappy
    cramjam_lz4 = _cramjam_lz4
    cramjam_snappy = _cramjam_snappy
except Exception as e:
    log.error("Error loading cramjam: %s", e)
    
import zlib

import ctypes
import subprocess
import sys
def get_dll_path(dll_name):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    dll_path = os.path.join(script_dir, dll_name)
    return dll_path

if sys.platform.startswith("linux"):
    _doboz_lib_name = "Doboz.so"
elif sys.platform == "darwin":
    _doboz_lib_name = "Doboz.dylib"
else:
    _doboz_lib_name = "Doboz.dll"

# Doboz is a native library, so it can only be shipped prebuilt for platforms we
# have a binary for (currently Windows). Everywhere else it is compiled on first
# use from the vendored sources in native/src/ and cached per user, which keeps
# binaries out of the repository. See native/README.md.
_DOBOZ_SRC_DIRNAME = 'src'
_DOBOZ_SOURCES = ('doboz_capi.cpp', 'Decompressor.cpp')
_DOBOZ_COMPILERS = ('g++', 'clang++', 'c++')


def _doboz_src_dir():
    return get_dll_path(os.path.join('native', _DOBOZ_SRC_DIRNAME))


def _doboz_cache_dir():
    """Writable per-user directory for the compiled library.

    The extension's own directory may be read-only, so prefer Blender's per-user
    extension data path. Falls back to a plain cache dir so that CR2W stays
    usable outside Blender.
    """
    parts = (__package__ or '').split('.')
    if len(parts) >= 3 and parts[0] == 'bl_ext':
        try:
            import bpy
            return bpy.utils.extension_path_user('.'.join(parts[:3]), create=True)
        except Exception:
            pass
    fallback = os.path.join(
        os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'),
        'witcher3_tools',
    )
    os.makedirs(fallback, exist_ok=True)
    return fallback


def _build_doboz_lib(dest_path):
    """Compile the vendored Doboz decompressor to dest_path.

    Returns the compiler used. Raises RuntimeError with the compiler's own
    diagnostics if the build fails, so the caller can surface something
    actionable.
    """
    src_dir = _doboz_src_dir()
    missing = [n for n in _DOBOZ_SOURCES if not os.path.isfile(os.path.join(src_dir, n))]
    if missing:
        raise RuntimeError(f"vendored Doboz sources are missing from {src_dir}: {', '.join(missing)}")

    compiler = next((c for c in _DOBOZ_COMPILERS if shutil.which(c)), None)
    if compiler is None:
        raise RuntimeError(
            "no C++ compiler found on PATH (looked for: " + ", ".join(_DOBOZ_COMPILERS) + ")"
        )

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    # Build to a temporary file first so a failed or concurrent build can never
    # leave a half-written library behind for the next run to load.
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=os.path.splitext(dest_path)[1], dir=os.path.dirname(dest_path))
    os.close(tmp_fd)
    try:
        cmd = [
            compiler, '-O2', '-fPIC', '-shared', '-DNDEBUG',
            # The upstream sources use size_t without including <cstddef>,
            # relying on it arriving transitively. Newer libstdc++ does not.
            '-include', 'cstddef',
            '-I', src_dir,
            '-o', tmp_path,
        ] + [os.path.join(src_dir, n) for n in _DOBOZ_SOURCES]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or '').strip()
            raise RuntimeError(f"{compiler} failed: {detail}")
        os.replace(tmp_path, dest_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return compiler


def _bind_doboz_lib(path):
    lib = ctypes.CDLL(path)
    lib.Decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
    lib.Decompress.restype = ctypes.c_int
    return lib


_doboz_lib = None
def get_doboz_lib():
    global _doboz_lib
    if _doboz_lib is not None:
        return _doboz_lib

    attempts = []

    # 1. A prebuilt library shipped alongside the addon (Windows).
    bundled = get_dll_path(os.path.join('native', _doboz_lib_name))
    if os.path.isfile(bundled):
        try:
            _doboz_lib = _bind_doboz_lib(bundled)
            return _doboz_lib
        except OSError as e:
            attempts.append(f"bundled library at {bundled}: {e}")

    # 2. A copy this addon compiled earlier.
    cached = os.path.join(_doboz_cache_dir(), _doboz_lib_name)
    if os.path.isfile(cached):
        try:
            _doboz_lib = _bind_doboz_lib(cached)
            return _doboz_lib
        except OSError as e:
            attempts.append(f"cached library at {cached}: {e}")

    # 3. Compile it from the vendored sources, once, and cache the result.
    try:
        compiler = _build_doboz_lib(cached)
    except Exception as e:
        attempts.append(f"building from source: {e}")
    else:
        try:
            _doboz_lib = _bind_doboz_lib(cached)
            log.info("Built Doboz decompressor with %s -> %s", compiler, cached)
            return _doboz_lib
        except OSError as e:
            attempts.append(f"library just built at {cached}: {e}")

    raise MissingCompressionException(
        "Doboz",
        "Doboz decompressor is unavailable. A C++ compiler is required to build it "
        "on this platform; install one (e.g. g++ or clang) and retry. Tried -- "
        + "; ".join(attempts)
    )

class MissingCompressionException(Exception):
    def __init__(self, compression, message="Unhandled compression algorithm."):
        self.compression = compression
        self.message = message
        super().__init__(self.message)

class BundleItem:
    def __init__(self, bundle = None, name = None, hash_val = None, empty = None, size = None, zsize = None, page_offset = None, timestamp = None, zero = None, crc = None, compression = None):
        self.bundle = bundle
        self.name = name
        self.hash = hash_val
        self.empty = empty
        self.size = size
        self.zsize = zsize
        self.page_offset = page_offset
        self.timestamp = timestamp
        self.zero = zero
        self.crc = crc
        self.compression = compression

    @property
    def compression_type(self):
        compression_mapping = {
            0: "None",
            1: "Zlib",
            2: "Snappy",
            3: "Doboz",
            4: "Lz4",
            5: "Lz4"
        }
        return compression_mapping.get(self.compression, "Unknown")

    def extract_existing_mmf(self, output, memorymappedbundle):
        start = self.page_offset
        end = start + self.zsize
        viewstream = memorymappedbundle[start:end]
        if self.compression_type == "None":
            output.write(viewstream)
        elif self.compression_type == "Lz4":
            if cramjam_lz4 is None:
                raise MissingCompressionException(self.compression, "LZ4 decompressor is unavailable.")
            try:
                # Bundles use LZ4 block format; try block first, then frame as a fallback.
                uncompressed_data = cramjam_lz4.decompress_block(viewstream, output_len=self.size)
            except Exception as e:
                try:
                    uncompressed_data = cramjam_lz4.decompress(viewstream, output_len=self.size)
                except Exception:
                    raise RuntimeError(f"LZ4 decompression failed: {e}") from e
            output.write(bytes(uncompressed_data))
        elif self.compression_type == "Snappy":
            if cramjam_snappy is None:
                raise MissingCompressionException(self.compression, "Snappy decompressor is unavailable.")
            try:
                # Bundles use raw Snappy blocks; try raw first, then framed as a fallback.
                uncompressed_data = cramjam_snappy.decompress_raw(viewstream, output_len=self.size)
            except Exception as e:
                try:
                    uncompressed_data = cramjam_snappy.decompress(viewstream, output_len=self.size)
                except Exception:
                    raise RuntimeError(f"Snappy decompression failed: {e}") from e
            output.write(bytes(uncompressed_data))
        elif self.compression_type == "Doboz":
            destination_buffer = ctypes.create_string_buffer(self.size)
            result = get_doboz_lib().Decompress(ctypes.byref(ctypes.create_string_buffer(viewstream)), self.zsize,
                                          ctypes.byref(destination_buffer),self.size)
            if result == 0:
                decompressed_data = bytearray(destination_buffer[:self.size])
                output.write(decompressed_data)
            else:
                log.error("Decompression failed with error code: %s", result)
                log.error("Input details: zsize=%s, size=%s, viewstream sample=%s", self.zsize, self.size, viewstream[:10])
                raise ValueError(f"Doboz decompression failed with error {result}")
                #print(f"Decompression failed with error code: {result}")
        elif self.compression_type == "Zlib":
            decompressor = zlib.decompressobj()
            uncompressed_data = decompressor.decompress(viewstream)
            output.write(uncompressed_data)
            output.write(decompressor.flush())
        else:
            raise MissingCompressionException(self.compression)

    def extract(self, output):
        with open(self.bundle.ArchiveAbsolutePath, 'rb') as f:
            mmapped_file = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            self.extract_existing_mmf(output, mmapped_file)
            mmapped_file.close()

    # def extract_to_file(self, file_name):
    #     os.makedirs(os.path.dirname(file_name), exist_ok=True)
    #     if os.path.exists(file_name):
    #         os.remove(file_name)
    #     with open(file_name, 'wb') as output:
    #         self.extract(output)
    #     return file_name
    def extract_to_file(self, file_name):
        if not file_name:
            raise ValueError("file_name cannot be empty")

        from ...common_blender import win_safe_path

        safe_name = win_safe_path(file_name)
        dir_name = os.path.dirname(safe_name)
        os.makedirs(dir_name, exist_ok=True)

        temp_fd, temp_path = tempfile.mkstemp(dir=dir_name)
        try:
            with os.fdopen(temp_fd, 'wb') as temp_file:
                self.extract(temp_file)
            shutil.move(temp_path, safe_name)
        except Exception as e:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise e
        return file_name
