"""Local playback of companion audio on the PC's default output device.

When SPEAKER=pc, every rendered clip is also played on the machine the
backend runs on, and the phone mutes itself — the audio still crosses the
LAN to drive the face's mouth timing, but the sound itself comes from a
real speaker (an Echo in Bluetooth mode, or a wired set). No cloud round
trip: this is the same desk-machine loop as everything else in the app.

Playback is deliberately best-effort. If the PC cannot play (non-Windows,
no output device), the /speak endpoint tells the phone to play as usual
rather than silencing the companion.
"""

import io
import logging
import math
import struct
import wave

logger = logging.getLogger(__name__)

# winsound keeps no Python reference to the buffer it plays with
# SND_MEMORY|SND_ASYNC; a module-level reference keeps the clip alive for
# the whole playback, and the next play() simply replaces it.
_last_played: bytes | None = None


def play(audio: bytes) -> bool:
    """Play WAV bytes on the default output device, replacing any clip
    still playing. Returns False when playback is not possible, so the
    caller can fall back to the phone speaker. Never raises."""
    if not audio:
        return False
    try:
        import winsound

        global _last_played
        _last_played = audio
        # SND_ASYNC: return immediately; a newer clip replaces the old one,
        # exactly how the phone-side audio element behaves. SND_NODEFAULT: a
        # failure must stay silent, not degrade into a system beep.
        winsound.PlaySound(
            audio,
            winsound.SND_MEMORY | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
        )
        return True
    except Exception as e:
        logger.warning("Local speaker playback failed: %s", e)
        return False


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