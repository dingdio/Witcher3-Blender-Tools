"""Debug free camera during gameplay, stepped at a fixed 60 Hz.

Angles are degrees: yaw about +Z (0 looks along +Y, positive turns left), pitch up, roll.
"""
import math

KEY = 5.0                      # value of a pressed key
LOOK_RATE = 20.0               # degrees per second per unit of look input
PAD_LOOK = 5.0
SMOOTH = 0.6                   # per-tick approach of the visible camera
FOV_RANGE = (5.0, 150.0)


class FreeCamera:
    def __init__(self, pos, yaw: float, fov: float = 52.0):
        self.pos = list(pos)
        self.rot = [yaw, 0.0, 0.0]                 # yaw, pitch, roll
        self.visible_pos = list(self.pos)
        self.visible_rot = list(self.rot)
        self.fov = fov
        self.forward = self.right = self.up = 0.0
        self.boost = 0.0
        self.look_yaw = self.look_pitch = 0.0
        self.roll = 0.0

    @classmethod
    def at_player(cls, x: float, y: float, z: float, yaw_radians: float):
        """EnableFreeCamera: two metres above the player, with his rotation."""
        return cls((x, y, z + 2.0), math.degrees(yaw_radians))

    def keys(self, forward: float, right: float, up: float, boost: bool, roll: float):
        """Held keys as -1/0/1 axes."""
        self.forward, self.right = forward * KEY, right * KEY
        self.up = up * KEY * 0.05
        self.boost = KEY if boost else 0.0
        self.roll = roll

    def pad(self, lx: float, ly: float, rx: float, ry: float, lt: float, up: float, roll: float):
        self.forward, self.right = ly, lx
        self.up = up * KEY * 0.05
        self.boost = lt * KEY
        self.look_yaw += -rx * PAD_LOOK
        self.look_pitch += ry * PAD_LOOK
        self.roll = roll

    def mouse(self, dx: float, dy: float):
        """Raw counts, dy up; applied over the next tick like the engine's per-frame look."""
        self.look_yaw -= dx
        self.look_pitch += dy

    def zoom(self, step: float):
        self.fov = min(max(self.fov + step, FOV_RANGE[0]), FOV_RANGE[1])

    def axes(self):
        """(forward, right) unit vectors of the target rotation, both in the XY plane rotated by pitch."""
        yaw, pitch = math.radians(self.rot[0]), math.radians(self.rot[1])
        cp = math.cos(pitch)
        forward = (-math.sin(yaw) * cp, math.cos(yaw) * cp, math.sin(pitch))
        right = (math.cos(yaw), math.sin(yaw), 0.0)
        return forward, right

    def tick(self, dt: float):
        if self.forward or self.right or self.up:
            speed = 5.0 + self.boost * 5.0
            forward, right = self.axes()
            for i in range(3):
                self.pos[i] += dt * speed * (forward[i] * self.forward + right[i] * self.right)
            self.pos[2] += dt * speed * self.up
        self.rot[0] += dt * self.look_yaw * LOOK_RATE
        self.rot[1] += dt * self.look_pitch * LOOK_RATE
        self.rot[2] += dt * self.roll * LOOK_RATE
        self.look_yaw = self.look_pitch = 0.0
        for i in range(3):
            self.visible_pos[i] += (self.pos[i] - self.visible_pos[i]) * SMOOTH
            self.visible_rot[i] += (self.rot[i] - self.visible_rot[i]) * SMOOTH
