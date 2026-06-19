"""Socket client for the Witcher Unreal import plugin."""

from __future__ import annotations

import json
import socket
from typing import Any

from .manifest import SCHEMA

_CONNECTION_LOST_WINERRORS = {10053, 10054, 10058}


def _connection_lost_winerror(exc: BaseException) -> int | None:
    winerror = getattr(exc, "winerror", None)
    if isinstance(winerror, int):
        return winerror
    errno_value = getattr(exc, "errno", None)
    if isinstance(errno_value, int) and errno_value in _CONNECTION_LOST_WINERRORS:
        return errno_value
    return None


def is_connection_lost_error(exc: BaseException) -> bool:
    if _connection_lost_winerror(exc) is not None:
        return True
    return isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError))


def _configure_import_socket(conn: socket.socket) -> None:
    try:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (AttributeError, OSError):
        pass
    try:
        # Windows defaults keepalive probes to a very long idle window. Layer
        # imports can leave the socket quiet while Unreal works, so ask for a
        # much shorter probe interval when the platform exposes it.
        conn.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 30_000, 10_000))
    except (AttributeError, OSError):
        pass


def _response_lost_payload(host: str, port: int, manifest_path: str, exc: BaseException) -> dict[str, Any]:
    address = f"{host}:{int(port)}"
    message = (
        "Unreal accepted the import request, but Blender lost the socket before "
        f"Unreal returned its completion response from {address}: {exc}"
    )
    return {
        "success": True,
        "response_lost": True,
        "request_sent": True,
        "warning": message,
        "warnings": [message],
        "manifest_path": manifest_path,
    }


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
    request_sent = False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as conn:
            _configure_import_socket(conn)
            conn.settimeout(timeout)
            conn.sendall(request)
            request_sent = True
            header = _recv_exact(conn, 4)
            size = int.from_bytes(header, byteorder="little", signed=False)
            body = _recv_exact(conn, size)
    except OSError as exc:
        if request_sent and is_connection_lost_error(exc):
            return _response_lost_payload(host, port, manifest_path, exc)
        raise
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
