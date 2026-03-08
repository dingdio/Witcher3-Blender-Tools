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
def get_dll_path(dll_name):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    dll_path = os.path.join(script_dir, dll_name)
    return dll_path
doboz_dll_path = get_dll_path(r'native\Doboz.dll')
doboz_lib = ctypes.CDLL(doboz_dll_path)
doboz_lib.Decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
doboz_lib.Decompress.restype = ctypes.c_int

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
            result = doboz_lib.Decompress(ctypes.byref(ctypes.create_string_buffer(viewstream)), self.zsize,
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
