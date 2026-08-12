from typing import Literal

from pydantic import BaseModel, Field, field_validator

# The six states window.Companion.setEmotion() accepts. Kept as a Literal so a
# malformed emotion from the LLM fails validation here instead of silently
# reaching the frontend and being ignored.
Emotion = Literal["idle", "happy", "think", "listen", "sad", "sleepy"]

EMOTIONS: tuple[str, ...] = ("idle", "happy", "think", "listen", "sad", "sleepy")


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=4000)

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content must not be blank")
        return v


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    # Client-held conversation history. Capped so a long session cannot grow
    # the prompt without bound; the service trims further before sending.
    history: list[ChatMessage] = Field(default_factory=list, max_length=40)

    @field_validator("message")
    @classmethod
    def message_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message must not be blank")
        return v


class ChatResponse(BaseModel):
    reply: str
    emotion: Emotion


class WakeRequest(BaseModel):
    """Posted by the wake-word device (ESP32) when it hears its keyword."""

    source: str = Field("unknown", max_length=64)
    # Device-side clock, informational only — the server trusts its own time
    # for debouncing, since the board's clock may be unset or drifting.
    timestamp: float | None = None


class WakeResponse(BaseModel):
    accepted: bool
    reason: str | None = None


class TranscribeResponse(BaseModel):
    # Empty when the clip held no real speech. The frontend treats that as
    # "nothing was said" and returns to idle rather than showing an error.
    text: str


class ReminderOut(BaseModel):
    id: int
    text: str
    # Epoch seconds; the frontend formats it in the device's own timezone.
    fire_time: float


class RemindersResponse(BaseModel):
    reminders: list[ReminderOut]


class FactOut(BaseModel):
    id: int
    text: str


class FactsResponse(BaseModel):
    facts: list[FactOut]


class AmbientResponse(BaseModel):
    """Glanceable info for the idle screen. Null when unavailable or unset —
    the frontend simply shows less rather than showing an error."""

    weather: str | None = None


class SpeakRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=600)

    @field_validator("text")
    @classmethod
    def text_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("text must not be blank")
        return v


class ConversationResponse(BaseModel):
    messages: list[ChatMessage]


class HealthResponse(BaseModel):
    status: str
    ollama_connected: bool
    model: str
    tts_enabled: bool = False
