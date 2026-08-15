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
    # The service normalises model output to this ceiling.  Keeping the API
    # contract bounded protects the small on-device bubble if a local model
    # ignores its prompt and emits a long completion.
    reply: str = Field(..., min_length=1, max_length=600)
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
    # NULL for one-shot; "daily" or "weekly" when it re-arms after firing.
    repeat: str | None = None
    # NULL normally; set when the reminder executes a PC power action
    # (scheduled sleep/shutdown/lock).
    power_action: str | None = None


class SnoozeReminderRequest(BaseModel):
    # A quick action should be useful without turning the notification into a
    # scheduling UI. The API still accepts longer intentional snoozes.
    minutes: int = Field(..., ge=1, le=1440)


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


class NowPlaying(BaseModel):
    title: str
    artist: str
    app: str
    status: str


class PCStatusResponse(BaseModel):
    """Read-only snapshot of the machine, for the dashboard.

    Every field is optional: a PC without a battery, a machine with nothing
    playing, or a non-Windows host should all render a partial panel rather
    than fail the whole request.
    """

    available: bool
    now_playing: NowPlaying | None = None
    volume_percent: int | None = None
    muted: bool | None = None
    cpu_percent: float | None = None
    ram_percent: float | None = None
    battery_percent: int | None = None
    battery_plugged: bool | None = None


class MediaActionRequest(BaseModel):
    # Transport only. Deliberately NOT the full pc_control.MEDIA_KEYS set: the
    # dashboard gets the actions a mis-click can undo by clicking again, which
    # is the same line /smart/devices/{id}/state is drawn on. Declaring it as a
    # Literal means FastAPI rejects anything else with a 422 before the request
    # ever reaches the keyboard-tapping code.
    action: Literal["play_pause", "next", "previous"]


class SmartDeviceOut(BaseModel):
    entity_id: str
    name: str
    domain: str
    state: str


class SmartDeviceStateRequest(BaseModel):
    turn_on: bool


class SmartDevicesResponse(BaseModel):
    # False when Home Assistant is disabled, untokened or unreachable — the
    # dashboard shows a setup hint instead of an empty list, which would
    # wrongly read as "you have no devices".
    available: bool
    devices: list[SmartDeviceOut] = []


class HealthResponse(BaseModel):
    status: str
    ollama_connected: bool
    model: str
    # Distinguishes an API that is reachable but still warming its local model
    # from one that is actually ready to answer.
    model_status: Literal["warming", "ready", "unavailable"]
    tts_enabled: bool = False
    ha_connected: bool = False
