"""Localhost listener so Unreal can trigger Blender-side actions."""

from __future__ import annotations

import json
import queue
import socket
import threading
import traceback

import bpy

from .socket_client import encode_message
from .terrain_unreal import UE_UNITS_PER_METER, unreal_to_w3_world

_HOST = "127.0.0.1"
_PORT = 40778

_server = None


class _Job:
    __slots__ = ("request", "event", "response")

    def __init__(self, request):
        self.request = request
        self.event = threading.Event()
        self.response = None


def _recv_exact(conn, size):
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = conn.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class _Listener(threading.Thread):
    def __init__(self, host, port):
        super().__init__(name="WitcherBlenderListener", daemon=True)
        self.host = host
        self.port = port
        self._sock = None
        self._running = False
        self._jobs = queue.Queue()

    def start_serving(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self._running = True
        self.start()
        bpy.app.timers.register(self._drain_jobs, persistent=True)

    def stop(self):
        self._running = False
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass

    # main thread
    def _drain_jobs(self):
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            try:
                job.response = _dispatch(job.request)
            except Exception as exc:
                job.response = {"success": False, "error": f"{exc}\n{traceback.format_exc()}"}
            job.event.set()
        return 0.1 if self._running else None

    def run(self):
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                if self._running:
                    continue
                break
            with conn:
                try:
                    self._serve_one(conn)
                except Exception:
                    pass

    def _serve_one(self, conn):
        header = _recv_exact(conn, 4)
        if header is None:
            return
        body = _recv_exact(conn, int.from_bytes(header, "little"))
        if body is None:
            return
        job = _Job(json.loads(body.decode("utf-8")))
        self._jobs.put(job)
        job.event.wait(timeout=600.0)
        conn.sendall(encode_message(job.response or {"success": False, "error": "timeout"}))


def _dispatch(request):
    command = str(request.get("command", ""))
    if command == "load_w2l_around_camera":
        return _load_w2l_around_camera(request)
    if command == "send_animation_to_blender":
        return _send_animation_to_blender(request)
    return {"success": False, "error": f"Unknown command: {command}"}


def _load_w2l_around_camera(request):
    from .operators import run_send_layers_around_camera

    camera_position = None
    cam = request.get("camera_unreal")
    if cam and len(cam) == 3:
        camera_position = unreal_to_w3_world(float(cam[0]), float(cam[1]), float(cam[2]))

    radius = request.get("radius")
    radius_m = float(radius) / UE_UNITS_PER_METER if radius is not None else None

    result = run_send_layers_around_camera(
        bpy.context, camera_position=camera_position, radius=radius_m, action="SEND"
    )
    return {
        "success": result["status"] == "FINISHED",
        "status": result["status"],
        "message": result["message"],
    }


def _send_animation_to_blender(request):
    from .unreal_animation import apply_unreal_animation_payload

    stats = apply_unreal_animation_payload(bpy.context, request)
    retarget_mode = str(stats.get("retarget_mode", "") or "")
    mode = "world-delta" if retarget_mode in {"world_delta_to_target", "component_world_delta_to_target"} else "legacy"
    apply_method = str(stats.get("apply_method", "") or "").replace("_", "-")
    track_source = str(stats.get("track_source", "") or "")
    source_label = ""
    if track_source == "GetAnimationPose":
        source_label = ", evaluated component" if stats.get("pose_space_source") == "component_tracks" else ", evaluated"
    message = (
        f"Loaded Unreal animation '{stats['name']}' on {stats['target']} "
        f"({stats['matched_tracks']}/{stats['source_tracks']} tracks, {stats['num_frames']} frames, "
        f"{mode}, {apply_method}{source_label})."
    )
    missing_core = list(stats.get("core_bones_missing_tracks") or ())
    if missing_core:
        message += f" Missing core tracks: {', '.join(str(name) for name in missing_core[:5])}."
    if (
        stats.get("retarget_mode") == "legacy_direct_source_local"
        and abs(float(stats.get("preview_facing_yaw_degrees", 0.0) or 0.0)) > 1e-6
    ):
        message += " Preview facing reversed."
    return {
        "success": True,
        "message": message,
        "stats": stats,
    }


def endpoint():
    return _HOST, _PORT


def is_running():
    return _server is not None and _server.is_alive()


def start():
    global _server
    if is_running():
        return ""
    server = _Listener(_HOST, _PORT)
    try:
        server.start_serving()
    except OSError as exc:
        print(f"[witcher] Blender listener failed to bind {_HOST}:{_PORT}: {exc}")
        return str(exc)
    _server = server
    return ""


def stop():
    global _server
    if _server is None:
        return
    _server.stop()
    _server = None
