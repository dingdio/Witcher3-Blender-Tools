from __future__ import annotations

import hashlib


def mesh_geometry_signature(obj) -> str:
    import numpy as np

    mesh = getattr(obj, "data", None)
    if mesh is None or not hasattr(mesh, "vertices"):
        return ""
    n = len(mesh.vertices)
    if n == 0:
        return "0"
    co = np.empty(n * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    quantized = np.rint(co * 1000.0).astype(np.int64)
    digest = hashlib.blake2b(quantized.tobytes(), digest_size=8).hexdigest()
    return f"{n}:{len(mesh.polygons)}:{digest}"
