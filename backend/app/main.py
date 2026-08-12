import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

# Route ALL TLS verification through the OS trust store, before anything that
# opens a connection is imported. services/tools.py builds its own context for
# the same reason, but third-party libraries construct their own — notably
# huggingface_hub, which downloads the Whisper weights and otherwise fails on
# the same incomplete-chain problem. Patching the stdlib covers every caller.
# Verification stays on; only the source of trusted roots changes.
import truststore

truststore.inject_into_ssl()

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

from app.config import settings
from app.models.schemas import (
    AmbientResponse,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ConversationResponse,
    FactOut,
    FactsResponse,
    HealthResponse,
    ReminderOut,
    RemindersResponse,
    SpeakRequest,
    TranscribeResponse,
    WakeRequest,
    WakeResponse,
)
from app.services import tools, tts
from app.services.companion import CompanionUnavailable, companion_service
from app.services.events import event_hub
from app.services.proactive import proactive_service
from app.services.reminders import reminder_service
from app.services.store import store
from app.services.transcription import TranscriptionError, transcription_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Proxies and mobile radios drop idle connections; a periodic comment line
# keeps the SSE stream alive without emitting a real event.
KEEPALIVE_INTERVAL_SECONDS = 15
KEEPALIVE_TICK = ": keep-alive\n\n"

# A keyword spotter commonly fires two or three times on one utterance, and the
# tail of the companion's own reply can retrigger it. Anything inside this
# window after an accepted wake is dropped.
WAKE_DEBOUNCE_SECONDS = 3.0
_last_wake_at = 0.0


def sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Starting Companion API (ollama=%s model=%s)",
        settings.ollama_base_url,
        settings.ollama_model,
    )
    store.init()
    # Reminders that came due while the server was down are NOT lost: they stay
    # pending in SQLite and fire once a client connects (see check_reminders).
    reminder_service.start()
    proactive_service.start()

    # Deliberately not awaited: with Ollama down this waits out its timeout,
    # and the server must be answering /health long before then. Held in a
    # local so the task is not garbage-collected mid-flight.
    prewarm_task = asyncio.create_task(companion_service.prewarm())

    yield

    prewarm_task.cancel()
    await proactive_service.stop()
    await reminder_service.stop()
    await companion_service.aclose()
    store.close()


app = FastAPI(
    title="Companion API",
    description="Chat core for the desk companion face",
    version="0.1.0",
    lifespan=lifespan,
)

# Local dev only. CORS_ORIGINS defaults to "*" because the frontend is opened
# straight from the filesystem or a throwaway static server, which gives it a
# null/shifting origin. Narrow this before the API is reachable off-machine.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthResponse)
async def health_check():
    return HealthResponse(
        status="ok",
        ollama_connected=await companion_service.is_available(),
        model=settings.ollama_model,
        tts_enabled=settings.tts_enabled,
    )


@app.post("/speak")
async def speak_endpoint(body: SpeakRequest):
    """Render a reply to speech. Returns WAV bytes.

    Separate from /chat on purpose: the chat contract stays {reply, emotion},
    and the frontend decides whether to ask for audio at all (it does not when
    the device is dimmed or sleeping).
    """
    if not settings.tts_enabled:
        raise HTTPException(status_code=503, detail="TTS is disabled")
    try:
        audio = await tts.synthesize(body.text)
    except tts.TTSError as e:
        logger.warning("TTS rejected: %s", e)
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.error("TTS failed: %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail="Speech synthesis failed") from e
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/reminders", response_model=RemindersResponse)
async def list_reminders():
    """Pending reminders, so they can be seen without asking out loud."""
    rows = await asyncio.to_thread(reminder_service.pending)
    return RemindersResponse(reminders=[ReminderOut(**row) for row in rows])


@app.delete("/reminders/{reminder_id}")
async def cancel_reminder(reminder_id: int):
    cancelled = await asyncio.to_thread(reminder_service.cancel, reminder_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail="No pending reminder with that id")
    return {"cancelled": reminder_id}


@app.get("/facts", response_model=FactsResponse)
async def list_facts():
    """What the companion durably knows. Visible on purpose — memory the user
    cannot inspect or delete is memory they cannot trust."""
    rows = await asyncio.to_thread(store.list_facts)
    return FactsResponse(facts=[FactOut(**row) for row in rows])


@app.delete("/facts/{fact_id}")
async def delete_fact(fact_id: int):
    if not await asyncio.to_thread(store.delete_fact, fact_id):
        raise HTTPException(status_code=404, detail="No such fact")
    return {"deleted": fact_id}


@app.delete("/facts")
async def clear_facts():
    removed = await asyncio.to_thread(store.clear_facts)
    logger.info("Cleared %d remembered fact(s)", removed)
    return {"cleared": removed}


# Ambient weather is polled by every idle screen, so it is cached rather than
# hitting Open-Meteo on each request. Weather does not move fast enough for a
# shorter window to tell anyone anything new.
_AMBIENT_TTL_SECONDS = 600.0
_ambient_cache: dict[str, tuple[float, str]] = {}


@app.get("/ambient", response_model=AmbientResponse)
async def ambient(city: str = ""):
    city = city.strip()
    if not city:
        return AmbientResponse(weather=None)

    cached = _ambient_cache.get(city.lower())
    if cached and time.monotonic() - cached[0] < _AMBIENT_TTL_SECONDS:
        return AmbientResponse(weather=cached[1])

    try:
        summary = tools.format_weather_compact(await tools.fetch_weather(city))
    except Exception as e:
        # The idle screen shows nothing rather than an error string.
        logger.info("Ambient weather unavailable for %r: %s", city, e)
        return AmbientResponse(weather=None)

    _ambient_cache[city.lower()] = (time.monotonic(), summary)
    return AmbientResponse(weather=summary)


@app.get("/conversation", response_model=ConversationResponse)
async def get_conversation():
    """Recent history, so a reloaded PWA does not start as a stranger."""
    rows = await asyncio.to_thread(store.recent_messages, settings.history_replay_limit)
    return ConversationResponse(messages=[ChatMessage(**row) for row in rows])


@app.delete("/conversation")
async def clear_conversation():
    removed = await asyncio.to_thread(store.clear_messages)
    logger.info("Cleared %d stored message(s)", removed)
    return {"cleared": removed}


@app.post("/wake", response_model=WakeResponse)
async def wake_endpoint(body: WakeRequest):
    """Wake trigger from the external keyword spotter.

    Pushes a 'wake' event onto the existing SSE stream; the frontend reacts by
    listening and auto-starting a recording. Debounced server-side so one
    utterance cannot start several recordings.
    """
    global _last_wake_at
    # Monotonic: immune to wall-clock adjustments, which matter on a machine
    # that may sync time while this is running.
    now = time.monotonic()
    since = now - _last_wake_at
    if since < WAKE_DEBOUNCE_SECONDS:
        logger.info("Wake from %s ignored (%.1fs since last)", body.source, since)
        return WakeResponse(accepted=False, reason="debounced")

    _last_wake_at = now
    event_hub.publish({"type": "wake", "emotion": "listen"})
    logger.info("Wake accepted from %s (%d listener(s))", body.source, event_hub.subscriber_count)
    return WakeResponse(accepted=True)


@app.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_endpoint(audio: UploadFile = File(...)):
    """Speech-to-text for tap-to-talk.

    Returns text only — the caller feeds it into POST /chat exactly as if the
    user had typed it, so the reply/emotion flow is untouched.
    """
    max_bytes = settings.max_audio_mb * 1024 * 1024
    try:
        data = await audio.read(max_bytes + 1)
    finally:
        await audio.close()

    if not data:
        raise HTTPException(status_code=400, detail="Empty audio upload")
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413, detail=f"Audio exceeds {settings.max_audio_mb} MB limit"
        )

    try:
        text = await transcription_service.transcribe(data)
    except TranscriptionError as e:
        logger.warning("Transcription rejected: %s", e)
        raise HTTPException(status_code=422, detail="Could not transcribe that audio") from e
    except Exception as e:
        logger.error("Transcription failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    logger.info("Transcribed %d bytes -> %r", len(data), text[:120])
    return TranscribeResponse(text=text)


@app.get("/events")
async def events(request: Request):
    """Server-sent events: reminders push through here.

    One queue per client (see EventHub); the finally block unsubscribes when
    the client goes away, which Starlette signals by closing the generator.
    """

    async def generate():
        queue = event_hub.subscribe()
        try:
            # Lets the frontend distinguish "connected" from "still dialling".
            yield sse_event({"type": "connected"})
            # Someone is listening again: deliver anything that came due while
            # the screen was off, without waiting up to a full poll interval.
            await asyncio.to_thread(reminder_service.check_reminders)
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=KEEPALIVE_INTERVAL_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield KEEPALIVE_TICK
                    continue
                yield sse_event(event)
        except asyncio.CancelledError:
            raise
        finally:
            event_hub.unsubscribe(queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Stops nginx-style proxies buffering the stream into uselessness.
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    # Resets the idle clock and the long-session clock (see proactive.py), so
    # the companion never checks in on someone it was just talking to.
    proactive_service.note_user_activity()
    try:
        result = await companion_service.chat(request.message, request.history)
    except CompanionUnavailable as e:
        # 503 rather than 500: the frontend treats this as "show the fallback
        # line and go sad", and it is genuinely a dependency being down.
        logger.warning("Chat unavailable: %s", e)
        raise HTTPException(status_code=503, detail="Companion is unavailable") from e
    except Exception as e:
        logger.error("Chat failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    # Persist only completed exchanges: a turn that errored was never seen by
    # the model, and replaying it as context would misrepresent the history.
    try:
        await asyncio.to_thread(store.append_message, "user", request.message)
        await asyncio.to_thread(store.append_message, "assistant", result["reply"])
    except Exception as e:
        # History is a convenience; never fail a good reply over it.
        logger.warning("Could not persist conversation: %s", e)

    return ChatResponse(**result)
