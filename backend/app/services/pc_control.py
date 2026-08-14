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
import re
import subprocess
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


# --- now playing ------------------------------------------------------------
# Winsdk enum values for GlobalSystemMediaTransportControlsSessionPlaybackStatus.
# Not exposed as friendly names by the binding, so mapped by hand.
_PLAYBACK_STATUS = {0: "closed", 1: "opened", 2: "changing", 3: "stopped", 4: "playing", 5: "paused"}


async def now_playing() -> dict | None:
    """The track the OS thinks is currently active, if any.

    Reads the same System Media Transport Controls session Windows' own
    volume flyout mini-player reads — so it reflects whatever app currently
    holds media focus (Spotify, a browser tab, VLC), with no per-app
    integration. Returns None when nothing is active, which is a normal
    outcome, not a failure.
    """
    _require_windows()
    import winsdk.windows.media.control as wmc

    manager = await wmc.GlobalSystemMediaTransportControlsSessionManager.request_async()
    session = manager.get_current_session()
    if session is None:
        return None

    info = await session.try_get_media_properties_async()
    playback = session.get_playback_info()
    return {
        "title": info.title or "",
        "artist": info.artist or "",
        "app": session.source_app_user_model_id or "",
        "status": _PLAYBACK_STATUS.get(playback.playback_status, "unknown"),
    }


# --- system stats -------------------------------------------------------------
# Cross-platform via psutil, unlike everything else in this module — kept here
# rather than a separate file since it is still "read the state of this
# machine," the same job as get_volume.


def system_stats() -> dict:
    import psutil

    battery = psutil.sensors_battery()
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "ram_percent": psutil.virtual_memory().percent,
        "battery_percent": round(battery.percent) if battery else None,
        "battery_plugged": battery.power_plugged if battery else None,
    }


# --- app launching ------------------------------------------------------------
# A fixed name -> path map from config, not a free-form path argument. The
# model never sees or invents a filesystem path; it only ever picks a name
# the user already approved in .env.


def launch_app(path: str) -> None:
    _require_windows()
    try:
        os_startfile(path)
    except OSError as e:
        raise PCControlError(f"could not launch it ({e})") from e


# --- open a browser tab -------------------------------------------------------
# Only http/https, and the check is a hard requirement rather than tidiness.
# Windows' ShellExecute (what os.startfile and webbrowser both end up calling)
# happily EXECUTES things: os.startfile("calc.exe") launches Calculator, and a
# file:// or ms-settings: target would open local files or system panels. The
# model chooses this argument, so without a scheme allowlist "open a tab"
# becomes "run an arbitrary program".
ALLOWED_URL_SCHEMES = {"http", "https"}

# Dot-separated labels only. Deliberately rejects anything with a space, a
# backslash or no dot at all — "cmd.exe /c dir" and "notepad" are not
# addresses, and should fail loudly rather than be silently turned into a
# nonsense URL.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\.?$",
    re.IGNORECASE,
)

# "calc.exe" satisfies the hostname pattern — ".exe" is shaped exactly like a
# TLD. Prepending https:// already makes it inert (the browser gets a URL and
# fails to resolve the host, it does not run anything), but relying on that is
# relying on a subtlety. Rejecting outright means the guarantee is visible in
# the code. ".com" is deliberately absent: it is a real TLD before it is a DOS
# executable.
_PROGRAM_SUFFIXES = (
    ".exe", ".bat", ".cmd", ".msi", ".ps1", ".vbs", ".scr",
    ".lnk", ".dll", ".hta", ".jar", ".sh", ".app",
)


def open_url(url: str) -> str:
    """Open a web URL in the default browser. Returns the URL actually opened."""
    _require_windows()
    import urllib.parse
    import webbrowser

    url = (url or "").strip()
    if not url:
        raise PCControlError("no address given")

    parsed = urllib.parse.urlparse(url)

    if parsed.scheme:
        # An explicit scheme must be one we allow. This is the check that
        # stops "javascript:", "file:", "data:" and "ms-settings:" — and note
        # it also catches Windows paths, since "C:/Windows/..." parses as
        # scheme "c".
        if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
            raise PCControlError(
                f"only http and https addresses can be opened, not '{parsed.scheme}:'"
            )
        if not parsed.netloc:
            raise PCControlError(f"'{url}' is not a valid web address")
        final = url
    else:
        # No scheme: accept it only if it genuinely looks like a bare
        # hostname, then assume https. Validating BEFORE prepending is the
        # point — prepending first would turn "javascript:alert(1)" into
        # something that passes a naive scheme check.
        host = url.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        if host.lower().endswith(_PROGRAM_SUFFIXES):
            raise PCControlError(f"'{url}' looks like a program, not a website")
        if not _HOSTNAME_RE.match(host):
            raise PCControlError(f"'{url}' is not a web address")
        final = "https://" + url

    if not webbrowser.open(final):
        raise PCControlError("no browser was available to open it")
    return final


def os_startfile(path: str) -> None:
    # A thin wrapper so tests can monkeypatch this one function rather than
    # the os module itself.
    import os

    os.startfile(path)  # noqa: S606 - path comes only from a config whitelist


# --- power --------------------------------------------------------------------
# Both go through the real Windows shutdown mechanism (no /f force flag), so
# apps get their normal chance to prompt for unsaved work rather than being
# killed outright. The confirmation gate lives in tools.py, one level up —
# these two are the actions themselves, executed only once that gate passes.


def sleep_pc() -> None:
    _require_windows()
    # SetSuspendState via rundll32 is the standard scripted-sleep incantation;
    # the ctypes powrprof binding is far fussier about argument marshalling
    # for comparatively little benefit here.
    result = subprocess.run(
        ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise PCControlError(f"Windows refused to sleep (exit {result.returncode})")


def shutdown_pc(delay_seconds: int = 5) -> None:
    _require_windows()
    result = subprocess.run(
        ["shutdown", "/s", "/t", str(max(0, delay_seconds))],
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise PCControlError(f"Windows refused to shut down (exit {result.returncode})")


def cancel_shutdown() -> None:
    """Abort a pending shutdown/restart scheduled by shutdown_pc."""
    _require_windows()
    subprocess.run(["shutdown", "/a"], capture_output=True, timeout=10)
