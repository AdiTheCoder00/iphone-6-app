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


def _grant_foreground() -> None:
    """Allow the process we are about to start to take the foreground.

    Windows refuses to hand a new window the focus when the launching process
    is in the background (foreground lock) — the browser then opens but just
    flashes its taskbar button. Granting the right beforehand is the
    documented fix. Best effort: the window still opens if it fails.
    """
    _require_windows()
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    except OSError:
        pass


# Window titles of common Chromium/Firefox builds, matched against when the
# URL fragments above do not cover whatever browser the user actually has.
_BROWSER_WINDOW_FRAGMENTS = ("chrome", "msedge", "edge", "firefox", "brave", "opera", "vivaldi")


def _foreground_in_background(fragments: list[str]) -> None:
    """Raise a window matching one of the fragments, without blocking the caller.

    Polling for the window can take a couple of seconds (a cold browser start),
    which must not stall the chat turn the tool call is part of — so the
    activation happens on a daemon thread.
    """
    if not IS_WINDOWS:
        return
    import threading

    threading.Thread(
        target=_bring_window_to_front, args=(fragments,), daemon=True
    ).start()


def _bring_window_to_front(fragments: list[str], timeout_seconds: float = 8.0) -> None:
    """Force a visible window whose title contains any fragment to the foreground.

    AllowSetForegroundWindow helps a freshly started process, but a browser
    that opens a tab in an ALREADY RUNNING window still ends up unfocused —
    the user sees the tab appear and the taskbar button flash. This is the
    standard workaround: attaching to the target window's input queue bypasses
    the foreground lock, after which SetForegroundWindow is honoured even from
    a background process. A simulated Alt press covers the remaining case.

    Best effort: if no matching window appears within the timeout, nothing
    happens and the caller's world is unchanged.
    """
    import ctypes
    import time
    from ctypes import wintypes

    fragments_lower = [f.lower() for f in fragments if f]
    if not fragments_lower:
        return

    enum_windows = ctypes.windll.user32.EnumWindows
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    get_window_text_length = ctypes.windll.user32.GetWindowTextLengthW
    get_window_text = ctypes.windll.user32.GetWindowTextW
    is_window_visible = ctypes.windll.user32.IsWindowVisible
    get_thread_process_id = ctypes.windll.user32.GetWindowThreadProcessId
    get_current_thread_id = ctypes.windll.kernel32.GetCurrentThreadId
    show_window = ctypes.windll.user32.ShowWindow
    set_foreground_window = ctypes.windll.user32.SetForegroundWindow
    bring_window_to_top = ctypes.windll.user32.BringWindowToTop
    attach_thread_input = ctypes.windll.user32.AttachThreadInput
    keybd_event = ctypes.windll.user32.keybd_event
    set_focus = ctypes.windll.user32.SetFocus

    SW_RESTORE = 9
    VK_MENU = 0x12
    KEYEVENTF_KEYUP = 0x0002

    def activate(hwnd) -> None:
        show_window(hwnd, SW_RESTORE)
        # A simulated Alt press resets the foreground lock so the
        # SetForegroundWindow below is honoured from this background process.
        # Harmless when not needed.
        keybd_event(VK_MENU, 0, 0, 0)
        keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        target_thread = get_thread_process_id(hwnd, None)
        own_thread = get_current_thread_id()
        attached = bool(
            target_thread
            and target_thread != own_thread
            and attach_thread_input(own_thread, target_thread, True)
        )
        set_foreground_window(hwnd)
        bring_window_to_top(hwnd)
        if attached:
            attach_thread_input(own_thread, target_thread, False)
        set_focus(hwnd)

    def find_and_activate() -> bool:
        found: list[int] = []

        def collect(hwnd, _lparam):
            if not is_window_visible(hwnd):
                return True
            length = get_window_text_length(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            get_window_text(hwnd, buf, length + 1)
            title = buf.value.lower()
            if any(frag in title for frag in fragments_lower):
                found.append(hwnd)
                return False  # stop enumerating
            return True

        enum_windows(enum_proc(collect), 0)
        if found:
            activate(found[0])
            return True
        return False

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if find_and_activate():
            return
        time.sleep(0.2)


def launch_app(path: str) -> None:
    _require_windows()
    _grant_foreground()
    try:
        os_startfile(path)
    except OSError as e:
        raise PCControlError(f"could not launch it ({e})") from e
    # Raise the app's window over whatever has focus, once it appears. The
    # basename is the best title fragment we have ("notepad.exe" -> a title
    # like "Untitled - Notepad").
    import os

    _foreground_in_background([os.path.splitext(os.path.basename(path))[0]])


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

    _grant_foreground()
    if not webbrowser.open(final):
        raise PCControlError("no browser was available to open it")
    # Bring the browser window over whatever has focus, once it appears. Match
    # on the registrable domain first ("youtube.com" -> a title like
    # "YouTube - Google Chrome"), then the full host, then any known browser.
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    fragments: list[str] = []
    labels = host.split(".")
    if len(labels) >= 2:
        fragments.append(labels[-2])
    fragments.append(host)
    fragments.extend(_BROWSER_WINDOW_FRAGMENTS)
    _foreground_in_background(fragments)
    return final


def os_startfile(path: str) -> None:
    # A thin wrapper so tests can monkeypatch this one function rather than
    # the os module itself.
    import os

    os.startfile(path)  # noqa: S606 - path comes only from a config whitelist


# --- file search & screenshots ------------------------------------------------
# Both go through Windows PowerShell because .NET's filesystem and drawing
# stacks are already on every Windows box — no new pip dependency, and no
# arbitrary command execution: the query text is escaped into a single-quoted
# PowerShell literal and the rest of the script is fixed.

_FILE_SEARCH_ROOTS = (
    r"$env:USERPROFILE\Desktop",
    r"$env:USERPROFILE\Documents",
    r"$env:USERPROFILE\Downloads",
)
_SEARCH_MAX_RESULTS = 5
_POWERSHELL_TIMEOUT = 20.0


def _ps_literal(value: str) -> str:
    """Escape a value for a single-quoted PowerShell literal."""
    return "'" + value.replace("'", "''") + "'"


def _run_powershell(script: str) -> str:
    """Run a fixed PowerShell script, returning stdout. Never raises."""
    import subprocess

    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=_POWERSHELL_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise PCControlError(f"powershell failed ({e})") from e
    return result.stdout


def find_files(name: str) -> list[str]:
    """Search Desktop/Documents/Downloads for files whose name contains `name`.

    Read-only, bounded to the user's own folders, and capped at five hits —
    a short list the model can read back, not an index of the disk.
    """
    _require_windows()
    name = (name or "").strip()
    if not name:
        raise PCControlError("find_files needs a name")
    # -like treats [ as a wildcard class; escape it so a literal bracket in
    # the query matches itself. * and ? in the query stay wildcards, which is
    # what someone searching for "report 202*" wants.
    pattern = name.replace("[", "`[").replace("]", "`]")
    script = (
        "$hits = @();"
        "Get-ChildItem -Path "
        + ",".join(_FILE_SEARCH_ROOTS)
        + " -Recurse -File -Force -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.Name -like '*{pattern}*' }} | "
        f"Select-Object -First {_SEARCH_MAX_RESULTS} | "
        "ForEach-Object { $hits += $_.FullName };"
        "$hits -join \"`n\""
    )
    out = _run_powershell(script)
    return [line.strip() for line in out.splitlines() if line.strip()]


def capture_screenshot(path: str) -> None:
    """Capture the primary screen to a PNG at `path`. Never raises."""
    _require_windows()
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
        "$bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height);"
        "$g = [System.Drawing.Graphics]::FromImage($bmp);"
        "$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size);"
        f"$bmp.Save({_ps_literal(path)}, [System.Drawing.Imaging.ImageFormat]::Png);"
        "$g.Dispose(); $bmp.Dispose()"
    )
    _run_powershell(script)


# --- clipboard ----------------------------------------------------------------
# CF_UNICODETEXT via user32/kernel32, no PowerShell and no encoding pitfalls.
# Clipboard memory is owned by the system once SetClipboardData succeeds, so
# the handles below must be NULLed (or never freed) on that path only.


def set_clipboard(text: str) -> None:
    """Copy text to the Windows clipboard, replacing whatever was there."""
    _require_windows()
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]

    data = (text or "").encode("utf-16-le") + b"\x00\x00"
    if not user32.OpenClipboard(None):
        raise PCControlError("could not open the clipboard")
    try:
        if not user32.EmptyClipboard():
            raise PCControlError("could not clear the clipboard")
        # GMEM_MOVEABLE | GMEM_ZEROINIT — the shape SetClipboardData expects.
        handle = kernel32.GlobalAlloc(0x0042, len(data))
        if not handle:
            raise PCControlError("could not allocate clipboard memory")
        transferred = False
        try:
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                raise PCControlError("could not lock clipboard memory")
            try:
                ctypes.memmove(ptr, data, len(data))
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(13, handle):  # CF_UNICODETEXT
                raise PCControlError("could not set clipboard data")
            transferred = True  # ownership now belongs to the system
        finally:
            if not transferred:
                kernel32.GlobalFree(handle)
    finally:
        user32.CloseClipboard()


def get_clipboard() -> str:
    """Read the current clipboard text, or '' when it holds no text."""
    _require_windows()
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]

    if not user32.OpenClipboard(None):
        raise PCControlError("could not open the clipboard")
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr).rstrip("\x00")
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


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
