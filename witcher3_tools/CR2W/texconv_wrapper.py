"""Minimal ctypes wrapper around texconv.dll (Texconv-Custom-DLL).

Used to decompress BC-compressed DDS files to TGA/PNG for editing in Blender.
The DLL is bundled in CR2W/third_party_libs/texconv.dll.

DLL source: https://github.com/matyalatte/Texconv-Custom-DLL (MIT license)
"""

import ctypes
import os
import logging

log = logging.getLogger(__name__)

_DLL = None


def _get_dll_path():
    """Return the path to the bundled texconv.dll."""
    libs_dir = os.path.join(os.path.dirname(__file__), "third_party_libs")
    dll_path = os.path.join(libs_dir, "texconv.dll")
    if os.path.isfile(dll_path):
        return dll_path
    return None


def _load_dll():
    """Load the texconv DLL, caching the handle globally."""
    global _DLL
    if _DLL is not None:
        return _DLL

    dll_path = _get_dll_path()
    if dll_path is None:
        raise RuntimeError(
            "texconv.dll not found in CR2W/third_party_libs/. "
            "DDS compression/decompression is unavailable."
        )

    _DLL = ctypes.cdll.LoadLibrary(dll_path)
    log.info("Loaded texconv.dll from %s", dll_path)
    return _DLL


def is_available():
    """Check whether texconv.dll is available."""
    return _get_dll_path() is not None


def convert_dds_to_tga(dds_path, output_dir=None, verbose=False):
    """Convert a DDS file to TGA using texconv.

    Args:
        dds_path: Path to the input .dds file.
        output_dir: Directory to write the output .tga file.
                    Defaults to the same directory as the input.

    Returns:
        Path to the output .tga file.
    """
    dll = _load_dll()

    if output_dir is None:
        output_dir = os.path.dirname(dds_path)

    os.makedirs(output_dir, exist_ok=True)

    args = ['-ft', 'tga', '-f', 'rgba']
    args += ['-y', '-o', output_dir, '--', os.path.normpath(dds_path)]

    return _run_texconv(dll, args, verbose=verbose, output_dir=output_dir,
                        input_path=dds_path, output_ext='tga')


def convert_dds_to_png(dds_path, output_dir=None, verbose=False):
    """Convert a DDS file to PNG using texconv.

    Args:
        dds_path: Path to the input .dds file.
        output_dir: Directory to write the output .png file.
                    Defaults to the same directory as the input.

    Returns:
        Path to the output .png file.
    """
    dll = _load_dll()

    if output_dir is None:
        output_dir = os.path.dirname(dds_path)

    os.makedirs(output_dir, exist_ok=True)

    args = ['-ft', 'png', '-f', 'rgba']
    args += ['-y', '-o', output_dir, '--', os.path.normpath(dds_path)]

    return _run_texconv(dll, args, verbose=verbose, output_dir=output_dir,
                        input_path=dds_path, output_ext='png')


def _run_texconv(dll, args, verbose=False, output_dir='.', input_path='', output_ext='tga'):
    """Execute texconv with the given arguments."""
    args_p = [ctypes.c_wchar_p(arg) for arg in args]
    args_p = (ctypes.c_wchar_p * len(args_p))(*args_p)
    err_buf = ctypes.create_unicode_buffer(512)

    result = dll.texconv(len(args), args_p, verbose, False, False, err_buf, 512)
    if result != 0:
        raise RuntimeError(f"texconv failed: {err_buf.value}")

    base_name = os.path.basename(input_path)
    base_name = '.'.join(base_name.split('.')[:-1] + [output_ext])
    output_path = os.path.join(output_dir, base_name)

    if not os.path.isfile(output_path):
        raise RuntimeError(f"texconv did not produce expected output: {output_path}")

    return output_path
