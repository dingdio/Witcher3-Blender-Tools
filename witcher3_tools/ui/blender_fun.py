import logging

log = logging.getLogger(__name__)
from mathutils import Euler
from math import radians
from ..CR2W.texture_converters import (
    convert_texarray_to_dds,
    convert_w2cube_to_dds,
    convert_xbm_to_dds,
)
from ..CR2W.texture_dds import is_valid_dds_file


def reset_transforms(new_obj):
    x, y, z = (radians(0), radians(0), radians(0))
    mat = Euler((x, y, z)).to_matrix().to_4x4()
    new_obj.matrix_world = mat
    new_obj.matrix_local = mat
    new_obj.matrix_basis = mat

    new_obj.location[0] = 0
    new_obj.location[1] = 0
    new_obj.location[2] = 0
    new_obj.scale[0] = 1
    new_obj.scale[1] = 1
    new_obj.scale[2] = 1

def _blender_image_has_data(image) -> bool:
    if image is None:
        return False
    try:
        size = tuple(getattr(image, "size", (0, 0)))
    except Exception:
        return False
    if len(size) < 2 or size[0] <= 0 or size[1] <= 0:
        return False
    has_data = getattr(image, "has_data", None)
    if has_data is not None and not has_data:
        return False
    return True


def _normalize_texture_repo_path(repo_path: str) -> str:
    import os

    normalized = str(repo_path or "").replace("/", "\\").lstrip("\\")
    if not normalized:
        return ""

    base, ext = os.path.splitext(normalized)
    if ext.lower() in {".dds", ".png", ".jpg", ".jpeg", ".tga", ".bmp"}:
        return base + ".xbm"
    return normalized


def _resolve_texture_repo_path_for_repair(image_path: str, image=None) -> str:
    repo_path = ""

    if image is not None:
        try:
            settings = getattr(image, "witcherui_TextureSettings", None)
            repo_path = getattr(settings, "repo_path", "") or ""
        except Exception:
            repo_path = ""

    repo_path = _normalize_texture_repo_path(repo_path)
    if repo_path:
        return repo_path

    try:
        from .ui_texture_export import resolve_texture_image_metadata

        repo_path, _texture_group = resolve_texture_image_metadata(
            bpy.context,
            image_path,
            repo_path=repo_path,
        )
    except Exception:
        repo_path = ""

    return _normalize_texture_repo_path(repo_path)


def _refresh_local_xbm_for_repair(image_path: str, image=None):
    import os
    from ..CR2W.common_blender import repo_file, win_safe_path, win_unprefix_path

    sibling_xbm_path = os.path.splitext(image_path)[0] + ".xbm"
    repo_path = _resolve_texture_repo_path_for_repair(image_path, image=image)
    last_error = None

    if repo_path:
        try:
            refreshed_xbm_path = win_unprefix_path(repo_file(repo_path))
            if refreshed_xbm_path and os.path.isfile(win_safe_path(refreshed_xbm_path)):
                return refreshed_xbm_path, None
        except Exception as exc:
            last_error = exc

    if os.path.isfile(win_safe_path(sibling_xbm_path)):
        return sibling_xbm_path, last_error

    return "", last_error


def _repair_dds_from_local_xbm(image_path: str, xbm_path: str = ""):
    import os
    from ..CR2W.common_blender import win_safe_path, win_unprefix_path

    xbm_path = win_unprefix_path(xbm_path or "")
    if not xbm_path:
        xbm_path = os.path.splitext(image_path)[0] + ".xbm"
    if not os.path.isfile(win_safe_path(xbm_path)):
        return False, None

    try:
        convert_xbm_to_dds(xbm_path, force=True, out_path=image_path)
    except Exception as exc:
        return False, exc

    return is_valid_dds_file(image_path), None


def load_image_with_dds_repair(image_path: str, *, image=None, check_existing=True, allow_dds_repair=False):
    import os
    from ..CR2W.common_blender import bpy_image_load_safe, win_unprefix_path

    image_path = win_unprefix_path(image_path or "")
    is_dds = image_path.lower().endswith(".dds")
    last_error = None
    source_image = image

    def _repair_dds() -> bool:
        nonlocal last_error
        if not (allow_dds_repair and is_dds):
            return False
        refreshed_xbm_path, refresh_error = _refresh_local_xbm_for_repair(image_path, image=source_image)
        if refresh_error is not None:
            last_error = refresh_error

        if refreshed_xbm_path:
            try:
                repaired, error = _repair_dds_from_local_xbm(image_path, xbm_path=refreshed_xbm_path)
            except Exception as exc:
                repaired, error = False, exc
            if repaired:
                return True
            if error is not None:
                last_error = error
        return False

    if is_dds and not is_valid_dds_file(image_path):
        _repair_dds()

    for attempt in range(2):
        loaded_image = None
        try:
            loaded_image = bpy_image_load_safe(image_path, check_existing=check_existing)
            try:
                loaded_image.reload()
            except Exception as exc:
                last_error = exc
            if _blender_image_has_data(loaded_image):
                return loaded_image, None
            if last_error is None:
                last_error = RuntimeError(
                    f"Blender failed to decode image data for {os.path.basename(image_path)}"
                )
        except Exception as exc:
            last_error = exc

        if attempt == 0 and _repair_dds():
            continue
        break

    return None, last_error


def load_w2cube_image(fdir, *, check_existing=True, colorspace='sRGB'):
    """Convert a .w2cube to DDS and load it as a Blender image.

    Returns `(image, dds_path)`. `image` can be `None` if conversion/load failed.
    """
    import os as _os
    from ..CR2W.common_blender import bpy_image_load_safe

    dds_path = convert_w2cube_to_dds(fdir)
    if not dds_path or not _os.path.exists(dds_path):
        return None, dds_path

    img = bpy_image_load_safe(dds_path, check_existing=check_existing)
    if img and colorspace:
        try:
            img.reload()
        except Exception:
            pass
        try:
            img.colorspace_settings.name = colorspace
        except Exception:
            pass
    return img, dds_path


_BLICK_EQ_FACE_W2_SUFFIX = {
    "front": "fr",
    "back": "bk",
    "left": "lf",
    "right": "rt",
    "up": "up",
    "down": "dn",
}
_BLICK_EQ_CUBEMAP_FACE_TO_NAME = {
    "PY": "front",   # +Y
    "NY": "back",    # -Y
    "NX": "left",    # -X
    "PX": "right",   # +X
    "PZ": "up",      # +Z
    "NZ": "down",    # -Z
}
_BLICK_EQ_FACE_ROTATIONS = {
    "front": (180.0, 0.0, 0.0),
    "back": (0.0, 180.0, 0.0),
    "up": (180.0, 0.0, 0.0),
    "down": (180.0, 0.0, 180.0),
    "right": (0.0, 180.0, 90.0),
    "left": (180.0, 0.0, 90.0),
}


def _blick_apply_face_rotation_np(pixels, rotation):
    """Apply REDengine -> Blender face-orientation correction to a cubemap face array."""
    x_rot, y_rot, z_rot = rotation

    if int(round(float(x_rot))) % 360 == 180:
        pixels = pixels[::-1, :, :]
    if int(round(float(y_rot))) % 360 == 180:
        pixels = pixels[:, ::-1, :]
    if z_rot:
        import numpy as np

        k = int(round(float(z_rot) / 90.0)) % 4
        if k:
            pixels = np.rot90(pixels, k=k)
    return pixels


def _blick_load_face_images_from_dds_files(face_files, *, check_existing=True):
    """Load exported cubemap face DDS files into `{front/back/left/right/up/down: np.ndarray}`."""
    import numpy as np
    from ..CR2W.common_blender import bpy_image_load_safe

    faces = {}
    for cube_face_key, face_name in _BLICK_EQ_CUBEMAP_FACE_TO_NAME.items():
        face_path = face_files.get(cube_face_key)
        if not face_path:
            continue

        img = bpy_image_load_safe(face_path, check_existing=check_existing)
        if not img:
            continue
        try:
            img.reload()
        except Exception:
            pass

        w, h = img.size
        if not w or not h:
            continue

        pixels = np.zeros(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(pixels)
        pixels = pixels.reshape(h, w, 4)

        # Blender stores image pixels bottom-to-top.
        pixels = np.flipud(pixels)

        rot = _BLICK_EQ_FACE_ROTATIONS.get(face_name, (0.0, 0.0, 0.0))
        if rot != (0.0, 0.0, 0.0):
            pixels = _blick_apply_face_rotation_np(pixels, rot)

        faces[face_name] = pixels

    return faces


def _blick_cubemap_to_equirectangular_np(faces, out_width=None, out_height=None):
    """Convert six corrected cubemap face arrays to an equirectangular RGBA image."""
    import numpy as np
    from math import pi

    sample_face = next(iter(faces.values()))
    face_h, face_w = sample_face.shape[:2]

    if out_width is None:
        out_width = face_w * 4
    if out_height is None:
        out_height = out_width // 2

    equirect = np.zeros((out_height, out_width, 4), dtype=np.float32)

    u = np.linspace(0.5 / out_width, 1.0 - 0.5 / out_width, out_width, dtype=np.float32)
    v = np.linspace(0.5 / out_height, 1.0 - 0.5 / out_height, out_height, dtype=np.float32)
    uu, vv = np.meshgrid(u, v)

    theta = (uu - 0.5) * (2.0 * pi)
    phi = (0.5 - vv) * pi

    x = np.cos(phi) * np.sin(theta)
    y = np.sin(phi)
    z = np.cos(phi) * np.cos(theta)

    abs_x = np.abs(x)
    abs_y = np.abs(y)
    abs_z = np.abs(z)

    eps = 1e-10
    face_defs = {
        "right": ((x > 0) & (abs_x >= abs_y) & (abs_x >= abs_z),
                  -z / np.where(x != 0, x, eps),
                  -y / np.where(x != 0, x, eps)),
        "left": ((x < 0) & (abs_x >= abs_y) & (abs_x >= abs_z),
                 z / np.where(abs_x != 0, abs_x, eps),
                 -y / np.where(abs_x != 0, abs_x, eps)),
        "up": ((y > 0) & (abs_y >= abs_x) & (abs_y >= abs_z),
               x / np.where(y != 0, y, eps),
               z / np.where(y != 0, y, eps)),
        "down": ((y < 0) & (abs_y >= abs_x) & (abs_y >= abs_z),
                 x / np.where(abs_y != 0, abs_y, eps),
                 -z / np.where(abs_y != 0, abs_y, eps)),
        "front": ((z > 0) & (abs_z >= abs_x) & (abs_z >= abs_y),
                  x / np.where(z != 0, z, eps),
                  -y / np.where(z != 0, z, eps)),
        "back": ((z < 0) & (abs_z >= abs_x) & (abs_z >= abs_y),
                 -x / np.where(abs_z != 0, abs_z, eps),
                 -y / np.where(abs_z != 0, abs_z, eps)),
    }

    for face_name, (mask, uc, vc) in face_defs.items():
        face_data = faces.get(face_name)
        if face_data is None:
            continue

        rows, cols = np.where(mask)
        if rows.size == 0:
            continue

        px = np.clip(((uc[mask] + 1.0) * 0.5 * (face_w - 1)).astype(np.int32), 0, face_w - 1)
        py = np.clip(((vc[mask] + 1.0) * 0.5 * (face_h - 1)).astype(np.int32), 0, face_h - 1)
        equirect[rows, cols] = face_data[py, px]

    return equirect


def _blick_store_equirect_image(equirect_data, image_name: str, *, colorspace='sRGB'):
    """Create/update a packed Blender image from an equirectangular numpy RGBA array."""
    import bpy
    import numpy as np

    h, w = equirect_data.shape[:2]
    img = bpy.data.images.get(image_name)
    if img is None:
        img = bpy.data.images.new(
            image_name,
            width=int(w),
            height=int(h),
            alpha=True,
            float_buffer=True,
        )
    else:
        if tuple(img.size) != (int(w), int(h)):
            try:
                img.scale(int(w), int(h))
            except Exception:
                pass

    flipped = np.flipud(equirect_data)
    img.pixels.foreach_set(flipped.ravel())
    try:
        img.update()
    except Exception:
        pass
    try:
        img.pack()
    except Exception:
        pass
    if colorspace:
        try:
            img.colorspace_settings.name = colorspace
        except Exception:
            pass
    return img


def load_w2cube_blick_equirect_image(fdir, *, check_existing=True, colorspace='sRGB'):
    """Convert a .w2cube into a packed equirectangular Blick image built from 6 exported face DDS files.

    Returns `(image, dds_path)`. Falls back to `(None, dds_path)` on equirect build failure.
    """
    import os as _os
    from pathlib import Path

    # Ensure the cubemap DDS exists first.
    _unused_img, dds_path = load_w2cube_image(fdir, check_existing=check_existing, colorspace=colorspace)
    if not dds_path or not _os.path.exists(dds_path):
        return None, dds_path

    # Reuse existing exported face files if present; otherwise export them.
    dds_stem = Path(dds_path).stem
    dds_parent = Path(dds_path).parent
    face_files = {}
    for cube_face_key, face_name in _BLICK_EQ_CUBEMAP_FACE_TO_NAME.items():
        suffix = _BLICK_EQ_FACE_W2_SUFFIX[face_name]
        candidate = str(dds_parent / f"{dds_stem}__{suffix}.dds")
        if _os.path.exists(candidate):
            face_files[cube_face_key] = candidate

    if len(face_files) < 6:
        try:
            from .ui_material import _export_cubemap_face_dds_files
            exported = _export_cubemap_face_dds_files(dds_path)
            if exported:
                face_files = exported
        except Exception:
            log.exception("Failed to export cubemap face DDS files for Blick equirect build: %s", dds_path)
            return None, dds_path

    if len(face_files) < 6:
        log.warning("Missing cubemap face DDS files for Blick equirect build: %s", dds_path)
        return None, dds_path

    try:
        faces = _blick_load_face_images_from_dds_files(face_files, check_existing=check_existing)
    except Exception:
        log.exception("Failed loading cubemap face DDS images for Blick equirect build: %s", dds_path)
        return None, dds_path

    if len(faces) < 6:
        missing = sorted(set(_BLICK_EQ_FACE_W2_SUFFIX.keys()) - set(faces.keys()))
        log.warning("Blick equirect build missing faces %s from %s", missing, dds_path)
        return None, dds_path

    try:
        sample = next(iter(faces.values()))
        eq_w = int(sample.shape[1]) * 4
        eq_h = eq_w // 2
        equirect = _blick_cubemap_to_equirectangular_np(faces, eq_w, eq_h)
    except Exception:
        log.exception("Failed converting cubemap faces to Blick equirect image: %s", dds_path)
        return None, dds_path

    image_name = f"{Path(fdir).stem}_BlickCubemap_Equirect"
    try:
        img = _blick_store_equirect_image(equirect, image_name, colorspace=colorspace)
        img["witcher_blick_equirect_source"] = str(fdir)
        img["witcher_blick_equirect_dds"] = str(dds_path)
        return img, dds_path
    except Exception:
        log.exception("Failed creating packed Blick equirect Blender image for %s", dds_path)
        return None, dds_path

