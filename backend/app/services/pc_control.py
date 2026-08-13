"""Local machine control — media transport, volume, lock screen.

Windows only, and registered as tools only when running on Windows (see
tools.py). Everything here acts on the machine the backend process is running
on, which is the whole point: the companion sits on the desk next to the PC it
controls.

Scope is deliberately a fixed whitelist of named actions with no free-form
arguments. There is no run-a-command tool and there should not be: tool
routing on a local 8B model measured 78-100% reliable, and arbitrary shell
execution behind a probabilistic router is a bad trade at any accuracy.

Nothing here is destructive. The worst outcome of a misrouted call is that
music pauses or the screen locks.
"""

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# Virtual key codes. Tapping these is exactly what a keyboard's media keys do,
# so whatever app currently owns media focus responds — Spotify, a browser
# tab, VLC — with no per-app integration.
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_VOLUME_MUTE = 0xAD

KEYEVENTF_KEYUP = 0x0002

MEDIA_KEYS = {
    "play_pause": VK_MEDIA_PLAY_PAUSE,
    "next": VK_MEDIA_NEXT_TRACK,
    "previous": VK_MEDIA_PREV_TRACK,
    "stop": VK_MEDIA_STOP,
}


class PCControlError(RuntimeError):
    """The action could not be performed on this machine."""


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise PCControlError("PC control is only implemented for Windows")


def _tap_key(vk: int) -> None:
    """Press and release a virtual key."""
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def media(action: str) -> str:
    _require_windows()
    key = MEDIA_KEYS.get((action or "").strip().lower())
    if key is None:
        raise PCControlError(
            f"unknown media action '{action}'. Use: {', '.join(MEDIA_KEYS)}"
        )
    _tap_key(key)
    return action


def _endpoint_volume():
    """Windows Core Audio endpoint for the default output device.

    COM must be initialised per thread, and these calls run in FastAPI's
    threadpool, so it is initialised on every call rather than once at import.
    """
    import comtypes
    from pycaw.utils import AudioUtilities

    comtypes.CoInitialize()
    return AudioUtilities.GetSpeakers().EndpointVolume


def get_volume() -> tuple[int, bool]:
    """Returns (percent, muted)."""
    _require_windows()
    ev = _endpoint_volume()
    return round(ev.GetMasterVolumeLevelScalar() * 100), bool(ev.GetMute())


def set_volume(percent: int) -> int:
    _require_windows()
    percent = max(0, min(100, int(percent)))
    ev = _endpoint_volume()
    # Setting a level does not clear an existing mute, which would look like
    # the command silently failed.
    if ev.GetMute():
        ev.SetMute(0, None)
    ev.SetMasterVolumeLevelScalar(percent / 100.0, None)
    return percent


def set_mute(muted: bool) -> bool:
    _require_windows()
    ev = _endpoint_volume()
    ev.SetMute(1 if muted else 0, None)
    return muted


def lock_screen() -> None:
    _require_windows()
    if not ctypes.windll.user32.LockWorkStation():
        # Fails when a screensaver/secure desktop already has the session.
        raise PCControlError("Windows refused the lock request")
