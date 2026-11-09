"""Optional Windows XInput gamepad reader."""
import ctypes
import math
import sys
import time

BUTTON_A, BUTTON_B, BUTTON_X, BUTTON_Y = 0x1000, 0x2000, 0x4000, 0x8000
BUTTON_LB, BUTTON_RB, BUTTON_START, BUTTON_BACK = 0x0100, 0x0200, 0x0010, 0x0020
BUTTON_LTHUMB, BUTTON_RTHUMB = 0x0040, 0x0080
BUTTON_DPAD_UP, BUTTON_DPAD_DOWN, BUTTON_DPAD_LEFT, BUTTON_DPAD_RIGHT = 0x0001, 0x0002, 0x0004, 0x0008
EMPTY_RETRY = 1.0              # polling an empty slot every frame is slow (XInput docs)
_dll = None
_missing = False
_retry = {}


class _Gamepad(ctypes.Structure):
    _fields_ = [("buttons", ctypes.c_ushort), ("lt", ctypes.c_ubyte), ("rt", ctypes.c_ubyte),
                ("lx", ctypes.c_short), ("ly", ctypes.c_short), ("rx", ctypes.c_short), ("ry", ctypes.c_short)]


class _State(ctypes.Structure):
    _fields_ = [("packet", ctypes.c_ulong), ("gamepad", _Gamepad)]


def _load():
    global _dll, _missing
    if _dll is None and not _missing and sys.platform == "win32":
        for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                _dll = ctypes.windll.LoadLibrary(name)
                break
            except OSError:
                continue
        _missing = _dll is None
    return _dll


def _stick(x: int, y: int, deadzone: float):
    fx, fy = x / 32767.0, y / 32767.0
    mag = math.hypot(fx, fy)
    if mag <= deadzone:
        return 0.0, 0.0
    k = min((mag - deadzone) / (1.0 - deadzone), 1.0) / mag
    return fx * k, fy * k


def read(index: int = 0, deadzone: float = 0.2):
    """{lx, ly, rx, ry (-1..1, y up), lt, rt (0..1), buttons} of pad `index`, None when absent."""
    dll = _load()
    if dll is None or time.perf_counter() < _retry.get(index, 0.0):
        return None
    state = _State()
    if dll.XInputGetState(index, ctypes.byref(state)) != 0:
        _retry[index] = time.perf_counter() + EMPTY_RETRY
        return None
    g = state.gamepad
    lx, ly = _stick(g.lx, g.ly, deadzone)
    rx, ry = _stick(g.rx, g.ry, deadzone)
    return {"lx": lx, "ly": ly, "rx": rx, "ry": ry, "lt": g.lt / 255.0, "rt": g.rt / 255.0, "buttons": g.buttons}
