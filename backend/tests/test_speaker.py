"""Tests for local PC speaker playback (SPEAKER=pc)."""

import io
import sys
import wave

import pytest

from app.services import speaker

SILENT_WAV: bytes = b""


def _silent_wav(seconds: float = 0.2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _fresh_last_played():
    speaker._last_played = None
    yield
    speaker._last_played = None


def test_play_rejects_empty_audio():
    assert speaker.play(b"") is False


def test_play_reports_failure_from_raw_layer(monkeypatch):
    monkeypatch.setattr(speaker, "_play_wav", lambda _audio: False)
    assert speaker.play(_silent_wav()) is False


def test_play_reports_success(monkeypatch):
    monkeypatch.setattr(speaker, "_play_wav", lambda _audio: True)
    assert speaker.play(_silent_wav()) is True


def test_play_never_raises(monkeypatch):
    def boom(_audio):
        raise RuntimeError("no audio service")

    monkeypatch.setattr(speaker, "_play_wav", boom)
    assert speaker.play(_silent_wav()) is False


def test_play_keeps_buffer_alive():
    """The buffer passed to PlaySoundW must outlive the call for async
    playback; the module-level reference is what guarantees that."""
    speaker.play(_silent_wav(0.01))
    assert speaker._last_played is not None


@pytest.mark.skipif(sys.platform != "win32", reason="PlaySoundW is Windows-only")
def test_play_sounds_on_windows():
    """Regression: winsound raises RuntimeError for SND_MEMORY|SND_ASYNC,
    which made SPEAKER=pc fall back to the phone on every clip. The ctypes
    path must actually start playback on a machine with an audio endpoint."""
    assert speaker.play(_silent_wav(0.05)) is True


def test_chime_wav_is_valid():
    wav = speaker._chime_wav()
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getframerate() == 22050
        # Two 0.22 s tones.
        assert w.getnframes() == 2 * int(22050 * 0.22)
