"""Socket client for the Witcher Unreal import plugin."""

from __future__ import annotations

import json
import socket
from typing import Any

from .manifest import SCHEMA


def encode_message(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return len(body).to_bytes(4, byteorder="little", signed=False) + body


def decode_message(data: bytes) -> dict[str, Any]:
    if len(data) < 4:
        raise ValueError("Response is shorter than the 4-byte header")
    size = int.from_bytes(data[:4], byteorder="little", signed=False)
    body = data[4:]
    if len(body) != size:
        raise ValueError(f"Response size mismatch: header={size}, body={len(body)}")
    return json.loads(body.decode("utf-8"))


def import_bundle_request(manifest_path: str) -> dict[str, Any]:
    return {
        "command": "import_bundle",
        "schema": SCHEMA,
        "manifest_path": manifest_path,
    }


def probe_import_server(host: str, port: int, *, timeout: float = 1.0) -> str:
    address = f"{host}:{int(port)}"
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return ""
    except ConnectionRefusedError:
        return (
            f"No Unreal import server is listening at {address}. "
            "Open the target Unreal project and make sure the Witcher Tools importer plugin is enabled."
        )
    except socket.timeout:
        return f"Timed out connecting to the Unreal import server at {address}."
    except OSError as exc:
        return f"Could not connect to the Unreal import server at {address}: {exc}"


def send_import_request(host: str, port: int, manifest_path: str, *, timeout: float = 600.0) -> dict[str, Any]:
    # Full character bundles (textures + masters + meshes) can take minutes in
    # the Unreal editor, so the response timeout is generous.
    request = encode_message(import_bundle_request(manifest_path))
    with socket.create_connection((host, int(port)), timeout=timeout) as conn:
        conn.settimeout(timeout)
        conn.sendall(request)
        header = _recv_exact(conn, 4)
        size = int.from_bytes(header, byteorder="little", signed=False)
        body = _recv_exact(conn, size)
    return json.loads(body.decode("utf-8"))


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = int(size)
    while remaining > 0:
        chunk = conn.recv(remaining)
        if not chunk:
            raise ConnectionError("Socket closed before response was complete")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
