"""Text-to-speech via kokoro-onnx (local, CPU, ONNX).

Same engine as guru-rag-app's read-aloud, trimmed to this app's job: one
English voice, short companion replies, no language switching. The browser's
own speechSynthesis was the obvious alternative and is deliberately not used —
iOS picks an arbitrary system voice per device, and a companion whose voice
changes depending on which phone it runs on has no character at all.

Model files live in TTS_MODEL_DIR. They are ~350 MB and download on first use
if absent.
"""

import asyncio
import concurrent.futures
import io
import logging
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/kokoro-v1.0.onnx"
)
_VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/voices-v1.0.bin"
)

# Replies are one or two sentences by construction; this only ever catches a
# runaway generation, and keeps a single synthesis bounded.
MAX_TTS_CHARS = 600

# Without a timeout a stalled connection to GitHub would pin the worker
# thread (and the load lock) forever, hanging every subsequent /speak.
DOWNLOAD_TIMEOUT_SECONDS = 60


class TTSError(Exception):
    """Synthesis failed, or produced no audio."""


_kokoro = None
# Module-level rather than lazily created: two concurrent first requests would
# otherwise each make their own lock and both load the model.
_load_lock = asyncio.Lock()

# TTS runs on its own single worker: a long load or download must not starve
# the shared default pool that /chat and the store calls use. Created only
# when TTS is enabled — a disabled TTS should never spin up a thread (or, via
# preload, download ~350 MB at boot).
_TTS_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = (
    concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")
    if settings.tts_enabled
    else None
)


def _model_dir() -> Path:
    return Path(settings.resolved_tts_model_dir)


def _download(url: str, dest: Path) -> None:
    """Stream to a .part file first, so an interrupted download never leaves
    a truncated file that looks complete on the next boot. One retry, then a
    clear failure — never an unbounded wait."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(
                url, timeout=DOWNLOAD_TIMEOUT_SECONDS
            ) as response, open(tmp, "wb") as out:
                shutil.copyfileobj(response, out, length=256 * 1024)
            tmp.replace(dest)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            logger.warning(
                "TTS download attempt %d of %s failed: %s", attempt, dest.name, e
            )
            if attempt == 1:
                # Back off a moment between attempts: on a flaky network two
                # immediate tries fail in the same second for nothing.
                time.sleep(2.0)
    if tmp.exists():
        tmp.unlink()
    raise TTSError(f"could not download {dest.name}")


def _init_kokoro():
    """Blocking: download if needed, then build the ONNX session."""
    from kokoro_onnx import Kokoro

    model_dir = _model_dir()
    model_dir.mkdir(parents=True, exist_ok=True)
    for url, name in ((_MODEL_URL, "kokoro-v1.0.onnx"), (_VOICES_URL, "voices-v1.0.bin")):
        local = model_dir / name
        if not local.is_file():
            logger.info("Downloading Kokoro model file %s (one-time, ~350 MB total)...", name)
            _download(url, local)
    return Kokoro(str(model_dir / "kokoro-v1.0.onnx"), str(model_dir / "voices-v1.0.bin"))


async def get_kokoro():
    global _kokoro
    if _kokoro is not None:
        return _kokoro
    async with _load_lock:
        if _kokoro is None:
            _kokoro = await asyncio.get_running_loop().run_in_executor(
                _TTS_EXECUTOR, _init_kokoro
            )
            logger.info("Kokoro TTS loaded (voice=%s)", settings.tts_voice)
    return _kokoro


async def preload() -> None:
    """Warm the Kokoro session at startup so the first /speak is instant.

    Mirrors the old LLM prewarm: without this, the first spoken reply after
    boot pays the whole model + phonemizer load. Never raises — on failure
    the lazy path in get_kokoro() loads on first use anyway.
    """
    if not settings.tts_enabled:
        logger.info("TTS disabled - skipping preload")
        return
    try:
        await get_kokoro()
    except Exception:
        logger.warning("TTS preload failed; will load on first use", exc_info=True)


async def shutdown() -> None:
    """Release the TTS worker thread at server teardown."""
    global _TTS_EXECUTOR
    if _TTS_EXECUTOR is not None:
        _TTS_EXECUTOR.shutdown(wait=True, cancel_futures=True)
        _TTS_EXECUTOR = None


async def synthesize(text: str) -> bytes:
    """Render `text` to WAV bytes."""
    text = (text or "").strip()
    if not text:
        raise TTSError("nothing to speak")
    if len(text) > MAX_TTS_CHARS:
        raise TTSError(f"text too long for TTS ({len(text)} > {MAX_TTS_CHARS} chars)")
    if _TTS_EXECUTOR is None:
        raise TTSError("TTS is disabled")

    kokoro = await get_kokoro()

    def _run() -> bytes:
        import soundfile as sf

        # LANGUAGE=hi pairs with a Hindi voice and lang tag (Kokoro ships
        # hf_alpha/hm_omega for Hindi); English keeps the configured voice.
        if settings.language == "hi":
            voice, lang = "hf_alpha", "hi"
        else:
            voice, lang = settings.tts_voice, "en-us"
        audio, sample_rate = kokoro.create(text, voice=voice, speed=settings.tts_speed, lang=lang)
        if len(audio) == 0:
            raise TTSError("TTS produced no audio")
        buf = io.BytesIO()
        # WAV rather than a compressed format: iOS Safari decodes it without
        # fuss, and these clips are a couple of seconds on a LAN.
        sf.write(buf, audio, sample_rate, format="WAV")
        return buf.getvalue()

    return await asyncio.get_running_loop().run_in_executor(_TTS_EXECUTOR, _run)
