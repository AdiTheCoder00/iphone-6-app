from pathlib import Path

from pydantic import ConfigDict, Field
from pydantic_settings import BaseSettings

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = ConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Local Ollama, same server guru-rag-app talks to.
    ollama_base_url: str = Field("http://localhost:11434", validation_alias="OLLAMA_BASE_URL")
    ollama_model: str = Field("qwen3:8b", validation_alias="OLLAMA_MODEL")

    # Companion replies are one or two sentences on a 375px screen, so the
    # ceiling is deliberately far below guru-rag-app's 700: it bounds a
    # runaway generation rather than shaping normal output.
    llm_max_tokens: int = Field(200, validation_alias="LLM_MAX_TOKENS")
    # Warmer than the RAG app's 0.3 — that one is grounded in retrieved text
    # and must not embellish; this one is small talk and should not sound
    # identical every time.
    llm_temperature: float = Field(0.7, validation_alias="LLM_TEMPERATURE")
    # Short replies on a warm model land in a few seconds; a cold model load
    # is the slow case this actually covers.
    llm_request_timeout: float = Field(60.0, validation_alias="LLM_REQUEST_TIMEOUT")

    # How long Ollama keeps the weights resident after a request. The default
    # is 5 minutes, which is exactly wrong for a desk companion: it is used in
    # short bursts with long gaps, so almost every conversation would open by
    # paying a ~12s cold load before generating a single token.
    ollama_keep_alive: str = Field("30m", validation_alias="OLLAMA_KEEP_ALIVE")
    # Load the weights at startup rather than on the first thing the user says.
    llm_prewarm_enabled: bool = Field(True, validation_alias="LLM_PREWARM_ENABLED")
    # Bounded so an absent or wedged Ollama leaves a stray task for seconds,
    # not for the full request timeout.
    llm_prewarm_timeout: float = Field(45.0, validation_alias="LLM_PREWARM_TIMEOUT")

    # Thinking models (qwen3 among them) spend their token budget in a
    # <think> block before answering, which both slows the reply and eats the
    # 200-token ceiling. Ollama exposes a per-request switch; leave it off
    # unless the configured model has no thinking mode to disable.
    llm_disable_thinking: bool = Field(True, validation_alias="LLM_DISABLE_THINKING")

    # Speech-to-text (tap-to-talk). faster-whisper, same engine as
    # guru-rag-app, but sized for the opposite job: a few seconds of speech
    # that must come back fast, not an hour of audio that must come back
    # accurate. "base" on CPU/int8 transcribes a 3s command in well under a
    # second; bump to "small"/"medium" (or device=cuda) if accuracy matters
    # more than latency on your machine.
    whisper_model_size: str = Field("base", validation_alias="WHISPER_MODEL_SIZE")
    whisper_device: str = Field("cpu", validation_alias="WHISPER_DEVICE")
    whisper_compute_type: str = Field("int8", validation_alias="WHISPER_COMPUTE_TYPE")
    # Pinning the language skips detection, which on a 3-second clip is both
    # slow and unreliable. Blank = autodetect.
    whisper_language: str = Field("en", validation_alias="WHISPER_LANGUAGE")
    # Greedy decoding. Beam search buys little on short commands and costs
    # latency the user feels directly.
    whisper_beam_size: int = Field(1, validation_alias="WHISPER_BEAM_SIZE")
    # Ceiling on an uploaded clip. The frontend caps recording at 15s, so this
    # only ever rejects something that did not come from it.
    max_audio_mb: int = Field(10, validation_alias="MAX_AUDIO_MB")

    # Text-to-speech (kokoro-onnx, local CPU). Model files (~350 MB) live in
    # TTS_MODEL_DIR and download on first use if absent.
    tts_enabled: bool = Field(True, validation_alias="TTS_ENABLED")
    tts_model_dir: str = Field("data/tts", validation_alias="TTS_MODEL_DIR")
    # af_heart is kokoro's warm English female voice. Full list in the
    # kokoro-onnx voices file; af_* / am_* are American, bf_* / bm_* British.
    tts_voice: str = Field("af_heart", validation_alias="TTS_VOICE")
    tts_speed: float = Field(1.0, validation_alias="TTS_SPEED")

    # Persistence. One SQLite file for reminders and conversation history.
    db_path: str = Field("data/companion.db", validation_alias="DB_PATH")
    # How many past messages to replay into a fresh page load.
    history_replay_limit: int = Field(20, validation_alias="HISTORY_REPLAY_LIMIT")

    # Proactive presence — the companion speaking first.
    proactive_enabled: bool = Field(True, validation_alias="PROACTIVE_ENABLED")
    proactive_morning_hour: int = Field(8, ge=0, le=23, validation_alias="PROACTIVE_MORNING_HOUR")
    # Wraps midnight when start > end, which is the normal configuration.
    proactive_quiet_start: int = Field(22, ge=0, le=23, validation_alias="PROACTIVE_QUIET_START")
    proactive_quiet_end: int = Field(7, ge=0, le=23, validation_alias="PROACTIVE_QUIET_END")
    # Hours of unbroken presence before a stretch/water nudge. 0 disables.
    proactive_session_hours: float = Field(3.0, validation_alias="PROACTIVE_SESSION_HOURS")
    # Hours of user silence before a check-in. 0 disables.
    proactive_idle_hours: float = Field(4.0, validation_alias="PROACTIVE_IDLE_HOURS")

    # FastAPI. Local dev only: "*" is fine while this runs on localhost, and
    # must be narrowed before the API is reachable from another machine.
    cors_origins: list[str] = Field(["*"], validation_alias="CORS_ORIGINS")

    # Relative paths resolve against the backend/ directory (the one holding
    # .env), matching guru-rag-app's convention.
    @property
    def resolved_tts_model_dir(self) -> str:
        p = Path(self.tts_model_dir)
        if not p.is_absolute():
            p = _ENV_FILE.parent / p
        return str(p)

    @property
    def resolved_db_path(self) -> str:
        p = Path(self.db_path)
        if not p.is_absolute():
            p = _ENV_FILE.parent / p
        return str(p)


settings = Settings()
