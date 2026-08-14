"""Speech-to-text for tap-to-talk, via a local faster-whisper model.

Same engine guru-rag-app uses, but tuned for the opposite workload: that one
transcribes hour-long Hindi discourses on a GPU, this one handles a few
seconds of English speech and has to feel instant on whatever the desk machine
is. Hence a small model on CPU/int8 by default.
"""

import asyncio
import io
import logging

from app.config import settings

logger = logging.getLogger(__name__)

# Whisper hallucinates confident text from silence; these are the canonical
# outputs it produces for an empty clip, and they must not reach the LLM.
_SILENCE_ARTEFACTS = {
    "you", "thank you.", "thank you", "thanks for watching!", "thanks for watching.",
    ".", "..", "...", "bye.", "bye", "you're welcome.", "[blank_audio]",
}


class TranscriptionError(RuntimeError):
    """Audio could not be decoded or transcribed."""


class TranscriptionService:
    def __init__(self) -> None:
        self._model = None
        # Model construction is blocking and not thread-safe; serialise the
        # first load so two simultaneous recordings cannot both build one.
        self._load_lock = asyncio.Lock()

    def _load_model(self):
        from faster_whisper import WhisperModel

        logger.info(
            "Loading Whisper (%s, device=%s, compute=%s)",
            settings.whisper_model_size,
            settings.whisper_device,
            settings.whisper_compute_type,
        )
        return WhisperModel(
            settings.whisper_model_size,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
        )

    async def _get_model(self):
        if self._model is None:
            async with self._load_lock:
                if self._model is None:      # re-check: another caller may have won
                    self._model = await asyncio.to_thread(self._load_model)
        return self._model

    def _transcribe_sync(self, model, audio: bytes) -> str:
        # faster-whisper decodes via PyAV, which handles the MP4/AAC that iOS
        # Safari's MediaRecorder produces as well as the WebM/Opus everything
        # else produces — so no container conversion is needed here.
        segments, _info = model.transcribe(
            io.BytesIO(audio),
            language=settings.whisper_language or None,
            beam_size=settings.whisper_beam_size,
            # A command is one breath of speech; VAD trims the dead air around
            # it so the model is not decoding room tone.
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    async def preload(self) -> None:
        """Warm the Whisper model at startup so the first tap-to-talk is
        instant, mirroring tts.preload(). Never raises — on failure the lazy
        path in _get_model() loads on first use anyway."""
        try:
            await self._get_model()
            logger.info("Whisper model loaded (size=%s)", settings.whisper_model_size)
        except Exception:
            logger.warning("Whisper preload failed; will load on first use", exc_info=True)

    async def transcribe(self, audio: bytes) -> str:
        """Return the transcript, or "" when the clip holds no real speech."""
        if not audio:
            raise TranscriptionError("empty audio")

        model = await self._get_model()
        try:
            text = await asyncio.to_thread(self._transcribe_sync, model, audio)
        except Exception as e:
            logger.error("Transcription failed: %s", e, exc_info=True)
            raise TranscriptionError(str(e)) from e

        # Treat a silence artefact as silence: better to do nothing than to
        # send the model a phantom "thank you".
        if text.strip().lower() in _SILENCE_ARTEFACTS:
            logger.info("Discarded silence artefact: %r", text)
            return ""
        return text


transcription_service = TranscriptionService()
