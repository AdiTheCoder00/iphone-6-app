"""Local playback of companion audio on the PC's default output device.

When SPEAKER=pc, every rendered clip is also played on the machine the
backend runs on, and the phone mutes itself — the audio still crosses the
LAN to drive the face's mouth timing, but the sound itself comes from a
real speaker (an Echo in Bluetooth mode, or a wired set). No cloud round
trip: this is the same desk-machine loop as everything else in the app.

Playback is deliberately best-effort. If the PC cannot play (non-Windows,
no output device), the /speak endpoint tells the phone to play as usual
rather than silencing the companion.

winsound refuses SND_MEMORY|SND_ASYNC ("Cannot play asynchronously from
memory") even though the OS supports it — that guard exists because Python
cannot promise to keep the buffer alive across the async call. PlaySoundW is
called directly through ctypes instead, and _last_played holds the buffer
for the whole playback; the next play() simply replaces it, exactly how the
phone-side audio element behaves. The BOOL return value is checked too: a
machine with no audio endpoint must not report success, or the phone would
mute itself into total silence. SND_NODEFAULT keeps a failure silent rather
than degrading into a system beep.
"""

import ctypes
import io
import logging
import math
import struct
import wave

logger = logging.getLogger(__name__)

_SND_MEMORY = 0x00000004
_SND_ASYNC = 0x00000001
_SND_NODEFAULT = 0x00000002

# Keeps the WAV buffer alive for the whole async playback; the next play()
# simply replaces it.
_last_played: bytes | None = None


def _play_wav(audio: bytes) -> bool:
    """Start async playback of WAV bytes, returning whether the sound could
    actually be started. Raises only on non-Windows or a bad buffer; a
    missing audio endpoint returns False rather than raising."""
    try:
        play_sound = ctypes.windll.winmm.PlaySoundW
    except AttributeError:
        # Not Windows — no local speaker to play on.
        return False
    play_sound.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint32]
    play_sound.restype = ctypes.c_int
    flags = _SND_MEMORY | _SND_ASYNC | _SND_NODEFAULT
    return bool(play_sound(audio, None, flags))


def play(audio: bytes) -> bool:
    """Play WAV bytes on the default output device, replacing any clip
    still playing. Returns False when playback is not possible, so the
    caller can fall back to the phone speaker. Never raises."""
    if not audio:
        return False
    global _last_played
    _last_played = audio
    try:
        started = _play_wav(audio)
    except Exception as e:  # defensive: playback must never take the app down
        logger.warning("Local speaker playback failed: %s", e)
        return False
    if not started:
        logger.warning("Local speaker playback could not start (no audio device?)")
    return started


def play_chime() -> bool:
    """Two-tone timer ding on the PC speaker, mirroring the phone-side
    chime. Same contract as play()."""
    return play(_chime_wav())


def _chime_wav() -> bytes:
    """Synthesise the timer ding (880 Hz then 660 Hz) as WAV bytes."""
    rate = 22050
    frames = bytearray()
    for freq in (880, 660):
        n = int(rate * 0.22)
        attack = int(rate * 0.02)
        for i in range(n):
            # Fast attack, squared decay — a clean ding with no click.
            env = min(1.0, i / attack) * (1.0 - i / n) ** 2
            sample = int(32767 * env * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", sample)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()
