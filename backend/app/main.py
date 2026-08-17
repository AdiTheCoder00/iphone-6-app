import asyncio
import base64
import json
import logging
import re
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings, unrecognized_env_keys
from app.models.schemas import (
    AmbientResponse,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ConversationResponse,
    FactOut,
    FactsResponse,
    HealthResponse,
    MediaActionRequest,
    NowPlaying,
    PCStatusResponse,
    ReminderOut,
    RemindersResponse,
    SmartDeviceOut,
    SmartDevicesResponse,
    SmartDeviceStateRequest,
    SnoozeReminderRequest,
    SpeakRequest,
    TranscribeResponse,
    WakeRequest,
    WakeResponse,
)
from app.middleware import CompanionTokenMiddleware
from app.services import smart_home, timers, tools, tts
from app.services.companion import (
    CompanionUnavailable,
    companion_service,
    describe_image,
)
from app.services.events import event_hub
from app.services.proactive import proactive_service
from app.services.reminders import reminder_event, reminder_service
from app.services.store import store
from app.services.transcription import TranscriptionError, transcription_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class _TokenRedactingFilter(logging.Filter):
    """Keep the shared secret out of access logs.

    /events accepts the token as a query parameter (EventSource cannot set
    headers), so uvicorn's access log would otherwise write it verbatim —
    and those logs live next to the repo on disk.

    The redaction must rewrite record.args, not record.request_line: uvicorn's
    AccessFormatter builds the final line from args (it copies the record and
    reconstructs request_line inside formatMessage, after filters have run).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if args and len(args) >= 3:
            path = str(args[2])
            path = re.sub(r"[?&]token=[^&\s]+", "", path)
            token = settings.companion_token
            if token:
                path = path.replace(token, "[REDACTED]")
            record.args = (*args[:2], path, *args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(_TokenRedactingFilter())

# Proxies and mobile radios drop idle connections; a periodic ping event keeps
# the SSE stream alive. It must be a REAL event, not a comment line: comment
# lines never reach the client's onmessage, so the phone-side stall watchdog
# would reconnect a healthy-but-quiet stream every few minutes.
KEEPALIVE_INTERVAL_SECONDS = 15

# A keyword spotter commonly fires two or three times on one utterance, and the
# tail of the companion's own reply can retrigger it. Anything inside this
# window after an accepted wake is dropped. The window is tracked per source:
# one board re-arming (or a second board) within the window must not swallow
# the other's wake.
WAKE_DEBOUNCE_SECONDS = 3.0
_last_wake_at: dict[str, float] = {}
# The dict is keyed by client-supplied text (token-gated and LAN-only, but
# still unbounded input): cap it so a chatty source cannot grow memory.
_MAX_TRACKED_WAKE_SOURCES = 16


def sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


KEEPALIVE_TICK = sse_event({"type": "ping"})


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized uploads from the Content-Length header before
    Starlette spools the body to disk. Without this, the /transcribe and
    /vision size caps run only after the whole upload has been written out,
    and anyone on the LAN could fill the disk with repeated large POSTs.

    Content-Length covers the whole multipart envelope for /vision (the
    image itself is capped again inside the endpoint). A chunked upload sends
    no Content-Length, so it is not stopped here — a streaming read cap would
    be needed for that, which Starlette's middleware API does not expose.
    """

    def __init__(self, app, max_bytes: int) -> None:
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request, call_next):
        if (
            request.method == "POST"
            and request.url.path in ("/transcribe", "/vision")
        ):
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > self._max_bytes:
                what = (
                    f"Audio exceeds {settings.max_audio_mb} MB limit"
                    if request.url.path == "/transcribe"
                    else f"Image exceeds {settings.max_image_mb} MB limit"
                )
                return JSONResponse({"detail": what}, status_code=413)
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Starting Companion API (groq model=%s)",
        settings.groq_chat_model,
    )
    if not settings.companion_token:
        logger.warning(
            "COMPANION_TOKEN is not set — every endpoint is open to anyone who can "
            "reach this port. PC control tools are reachable unauthenticated."
        )
    unknown = unrecognized_env_keys()
    if unknown:
        logger.warning("Unrecognized keys in backend/.env (possible typos): %s", ", ".join(unknown))
    store.init()
    # A crash between "claim" and "publish" would leave reminders fired but
    # never delivered; re-arm those before the poller starts so the next scan
    # delivers them.
    reminder_service.reconcile()
    # Reminders that came due while the server was down are NOT lost: they stay
    # pending in SQLite and fire once a client connects (see check_reminders).
    reminder_service.start()
    timers.timer_service.start()
    proactive_service.start()

    # Same reasoning for TTS: warm the Kokoro session in the background so the
    # first /speak after boot does not pay the load. Never raises. preload()
    # itself no-ops when TTS is disabled, so no download happens then either.
    tts_preload_task = asyncio.create_task(tts.preload())
    # Same again for Whisper: the first tap-to-talk after boot should not pay
    # the model load. Never raises; lazy path loads on first use if skipped.
    whisper_preload_task = asyncio.create_task(transcription_service.preload())

    yield

    tts_preload_task.cancel()
    whisper_preload_task.cancel()
    # Give the cancelled preload requests a chance to release their HTTP
    # resources before the shared clients are closed below. Awaiting them
    # also prevents a pending-task warning during a fast server restart.
    with suppress(asyncio.CancelledError):
        await tts_preload_task
    with suppress(asyncio.CancelledError):
        await whisper_preload_task
    await tts.shutdown()
    await proactive_service.stop()
    await reminder_service.stop()
    await timers.timer_service.stop()
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
# Methods and headers are listed explicitly, NOT wildcarded: the phone runs
# iOS 12, whose Safari rejects "*" in Access-Control-Allow-Methods/-Headers
# (wildcards landed in Safari 13.1) — every request carrying X-Companion-Token
# would fail its preflight and the face would report the backend unreachable
# even though the token-less EventSource stream connects fine.
app.add_middleware(CompanionTokenMiddleware)
# Registered after the token check so Starlette wraps CORS outermost: preflight
# requests are answered before auth, which they must be — a browser sends
# OPTIONS without custom headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["X-Companion-Token", "Content-Type", "Accept"],
)
# Registered last so it runs outermost: reject before auth, spooling or CORS
# handling — the check is header-only and reveals nothing.
app.add_middleware(
    BodySizeLimitMiddleware,
    # One shared bound for both upload paths; the endpoint caps below are the
    # authoritative per-path limits, so the middleware uses the larger of the
    # two rather than rejecting valid images when max_image_mb > max_audio_mb.
    max_bytes=max(settings.max_audio_mb, settings.max_image_mb) * 1024 * 1024,
)


async def _health_probe(coro) -> bool:
    """Run one health probe; a raised probe degrades to False."""
    try:
        return bool(await coro)
    except Exception as e:
        logger.warning("Health probe failed: %s", e)
        return False


@app.get("/health", response_model=HealthResponse)
async def health_check():
    # Probes run in parallel: the frontend aborts after 5s, and sequential
    # probes (Groq 3s + HA 3s) could exceed that on a wedged dependency.
    # One probe raising must not take the whole health check down — a health
    # endpoint that 500s is useless — so each failure degrades to False.
    llm_ok, ha_ok = await asyncio.gather(
        _health_probe(companion_service.is_available()),
        _health_probe(smart_home.is_available()),
    )
    return HealthResponse(
        status="ok",
        llm_connected=llm_ok,
        model=settings.groq_chat_model,
        model_status=companion_service.model_status,
        tts_enabled=settings.tts_enabled,
        ha_connected=ha_ok,
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
    return {"removed": reminder_id}


@app.post("/reminders/{reminder_id}/snooze", response_model=ReminderOut)
async def snooze_reminder(reminder_id: int, body: SnoozeReminderRequest):
    """Snooze an already-delivered reminder from its notification bubble."""
    reminder = await asyncio.to_thread(reminder_service.snooze, reminder_id, body.minutes)
    if reminder is None:
        raise HTTPException(status_code=409, detail="Reminder has already been handled")
    return ReminderOut(**reminder)


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
    return {"removed": fact_id}


@app.delete("/facts")
async def clear_facts():
    removed = await asyncio.to_thread(store.clear_facts)
    logger.info("Cleared %d remembered fact(s)", removed)
    return {"removed": removed}


# Ambient weather is polled by every idle screen, so it is cached rather than
# hitting Open-Meteo on each request. Weather does not move fast enough for a
# shorter window to tell anyone anything new.
_AMBIENT_TTL_SECONDS = 600.0
MAX_AMBIENT_CACHE_ENTRIES = 100
_ambient_cache: dict[str, tuple[float, str]] = {}


def _cache_ambient(city: str, summary: str, now: float) -> None:
    """Store a compact weather result without allowing arbitrary city input to
    grow this long-lived process cache forever."""
    expired = [key for key, (saved_at, _) in _ambient_cache.items()
               if now - saved_at >= _AMBIENT_TTL_SECONDS]
    for key in expired:
        _ambient_cache.pop(key, None)

    # Dicts preserve insertion order, so the first entry is the least recently
    # added cache value.  A bounded cache is enough here: weather is only an
    # ambient convenience and a later request can always refresh an evicted city.
    while len(_ambient_cache) >= MAX_AMBIENT_CACHE_ENTRIES and city not in _ambient_cache:
        _ambient_cache.pop(next(iter(_ambient_cache)))
    _ambient_cache[city] = (now, summary)


@app.get("/ambient", response_model=AmbientResponse)
async def ambient(city: str = Query("", max_length=100)):
    city = city.strip()
    if not city:
        return AmbientResponse(weather=None)

    cache_key = city.casefold()
    now = time.monotonic()
    cached = _ambient_cache.get(cache_key)
    if cached and now - cached[0] < _AMBIENT_TTL_SECONDS:
        return AmbientResponse(weather=cached[1])

    try:
        summary = tools.format_weather_compact(await tools.fetch_weather(city))
    except Exception as e:
        # The idle screen shows nothing rather than an error string. Expected
        # failures (unknown city, remote down) are routine; anything else is a
        # real defect that should surface instead of vanishing at INFO level.
        if isinstance(e, (ValueError, LookupError, httpx.HTTPError)):
            logger.info("Ambient weather unavailable for %r: %s", city, e)
        else:
            logger.warning("Ambient weather failed unexpectedly for %r: %s", city, e, exc_info=True)
        return AmbientResponse(weather=None)

    _cache_ambient(cache_key, summary, now)
    return AmbientResponse(weather=summary)


@app.get("/status/pc", response_model=PCStatusResponse)
async def pc_status():
    """Read-only snapshot of this machine, for the dashboard.

    Deliberately read-only. The dashboard never exposes lock/sleep/shutdown —
    those stay behind the companion's spoken confirmation flow rather than
    becoming a button that a mis-click can fire.

    Each part is fetched independently so one failing subsystem (no audio
    device, no media session) degrades that field to null instead of losing
    the whole panel.
    """
    from app.services import pc_control

    if not pc_control.IS_WINDOWS or not settings.pc_control_enabled:
        return PCStatusResponse(available=False)

    result = PCStatusResponse(available=True)

    try:
        track = await pc_control.now_playing()
        if track:
            result.now_playing = NowPlaying(**track)
    except Exception as e:
        logger.info("Dashboard now-playing unavailable: %s", e)

    try:
        level, muted = await asyncio.to_thread(pc_control.get_volume)
        result.volume_percent, result.muted = level, muted
    except Exception as e:
        logger.info("Dashboard volume unavailable: %s", e)

    try:
        stats = await asyncio.to_thread(pc_control.system_stats)
        result.cpu_percent = stats["cpu_percent"]
        result.ram_percent = stats["ram_percent"]
        result.battery_percent = stats["battery_percent"]
        result.battery_plugged = stats["battery_plugged"]
    except Exception as e:
        logger.info("Dashboard system stats unavailable: %s", e)

    return result


@app.post("/pc/media")
async def pc_media(body: MediaActionRequest):
    """Play/pause, next, previous — the dashboard's transport control.

    The read-only rule above still holds for lock/sleep/shutdown. Transport is
    the exception for the same reason a lamp toggle is: pressing play again
    undoes a mis-clicked pause, so there is nothing here a stray click can do
    that the next click cannot take back. The action set is closed by the
    request schema rather than by this function.
    """
    from app.services import pc_control

    if not pc_control.IS_WINDOWS or not settings.pc_control_enabled:
        raise HTTPException(status_code=503, detail="PC control is not available on this host")
    try:
        # Tapping a virtual key is a blocking user32 call; keep it off the loop.
        await asyncio.to_thread(pc_control.media, body.action)
    except pc_control.PCControlError as e:
        logger.warning("Dashboard media action %s failed: %s", body.action, e)
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"action": body.action}


@app.get("/smart/devices", response_model=SmartDevicesResponse)
async def smart_devices():
    """Smart home devices and their on/off state. Read-only."""
    if smart_home.provider() == "none":
        return SmartDevicesResponse(available=False)
    try:
        devices = await smart_home.list_devices()
    except smart_home.SmartHomeError as e:
        logger.info("Dashboard smart devices unavailable: %s", e)
        return SmartDevicesResponse(available=False)
    return SmartDevicesResponse(
        available=True, devices=[SmartDeviceOut(**d) for d in devices]
    )


@app.post("/smart/devices/{entity_id}/state")
async def set_smart_device_state(entity_id: str, body: SmartDeviceStateRequest):
    """Turn one device on or off.

    Exposed to the dashboard because it is trivially reversible — the worst a
    mis-click does is switch a lamp, and switching it back is one more click.
    That is the line for what gets a button here.
    """
    if smart_home.provider() == "none":
        raise HTTPException(status_code=503, detail="No smart home provider is configured")
    try:
        await smart_home.set_state(entity_id, body.turn_on)
    except smart_home.SmartHomeError as e:
        logger.warning("Smart device control failed for %s: %s", entity_id, e)
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"entity_id": entity_id, "turn_on": body.turn_on}


@app.get("/conversation", response_model=ConversationResponse)
async def get_conversation():
    """Recent history, so a reloaded PWA does not start as a stranger."""
    rows = await asyncio.to_thread(store.recent_messages, settings.history_replay_limit)
    return ConversationResponse(messages=[ChatMessage(**row) for row in rows])


@app.delete("/conversation")
async def clear_conversation():
    removed = await asyncio.to_thread(store.clear_messages)
    logger.info("Cleared %d stored message(s)", removed)
    return {"removed": removed}


@app.post("/wake", response_model=WakeResponse)
async def wake_endpoint(body: WakeRequest):
    """Wake trigger from the external keyword spotter.

    Pushes a 'wake' event onto the existing SSE stream; the frontend reacts by
    listening and auto-starting a recording. Debounced server-side so one
    utterance cannot start several recordings.
    """
    global _last_wake_at

    # A wake published into the void is wasted, and consuming the debounce for
    # it would silently swallow a real wake arriving within the window. Refuse
    # instead, so the board's serial log shows a "no-listener" refusal.
    if event_hub.subscriber_count == 0:
        logger.info("Wake from %s ignored (no SSE client connected)", body.source)
        return WakeResponse(accepted=False, reason="no-listener")

    # Monotonic: immune to wall-clock adjustments, which matter on a machine
    # that may sync time while this is running. Keyed by source so an
    # unrelated wake (a second board, or one that re-armed) is not dropped
    # just because another board spoke in the last few seconds.
    now = time.monotonic()
    since = now - _last_wake_at.get(body.source, 0.0)
    if since < WAKE_DEBOUNCE_SECONDS:
        logger.info("Wake from %s ignored (%.1fs since last)", body.source, since)
        return WakeResponse(accepted=False, reason="debounced")

    _last_wake_at[body.source] = now
    if len(_last_wake_at) > _MAX_TRACKED_WAKE_SOURCES:
        # Drop the oldest entry, keeping the dict bounded. A dropped source
        # simply loses its debounce for one wake — harmless.
        oldest = min(_last_wake_at, key=_last_wake_at.get)
        del _last_wake_at[oldest]
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

    # Length only — the transcript itself is user speech and does not belong
    # in the server log.
    logger.info("Transcribed %d bytes of audio", len(data))
    return TranscribeResponse(text=text)


@app.get("/screenshot")
async def screenshot_endpoint():
    """A PNG of the PC's primary screen, for 'what's on my screen'."""
    from app.services import pc_control

    if not pc_control.IS_WINDOWS or not settings.pc_control_enabled:
        raise HTTPException(status_code=404, detail="PC control is not available")
    # A unique name per request: a fixed path would let two concurrent calls
    # clobber each other mid-write. The FileResponse owns the file once handed
    # over (its BackgroundTask unlinks it after the stream); every other exit
    # path — a capture error, a client disconnect mid-capture — must clean up
    # or the temp dir accumulates screenshots.
    shot = Path(tempfile.gettempdir()) / f"companion_screenshot_{uuid.uuid4().hex}.png"
    delivered = False
    try:
        try:
            await asyncio.to_thread(pc_control.capture_screenshot, str(shot))
        except pc_control.PCControlError as e:
            logger.warning("Screenshot failed: %s", e)
            raise HTTPException(status_code=502, detail="Could not capture the screen") from e
        if not shot.is_file():
            raise HTTPException(status_code=502, detail="Could not capture the screen")
        delivered = True
        return FileResponse(
            shot,
            media_type="image/png",
            filename="screen.png",
            background=BackgroundTask(shot.unlink, missing_ok=True),
        )
    finally:
        if not delivered:
            shot.unlink(missing_ok=True)


def _guess_image_mime(data: bytes) -> str:
    """Sniff a photo's MIME type from its magic bytes.

    Groq's vision API needs the image wrapped in a data URL with the right
    content type; the phone uploads without one, so sniff it here. JPEG is
    the fallback — that is what a phone camera emits.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


@app.post("/vision")
async def vision_endpoint(image: UploadFile = File(...)):
    """Describe a photo taken on the phone, via the Groq vision model.

    The image is the whole context — no conversation, no tools — and the reply
    is plain text the phone renders like any other companion line.
    """
    max_bytes = settings.max_image_mb * 1024 * 1024
    try:
        data = await image.read(max_bytes + 1)
    finally:
        await image.close()

    if not data:
        raise HTTPException(status_code=400, detail="Empty image upload")
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413, detail=f"Image exceeds {settings.max_image_mb} MB limit"
        )

    try:
        text = await describe_image(
            base64.b64encode(data).decode(), _guess_image_mime(data)
        )
    except CompanionUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        logger.error("Vision failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    logger.info("Described %d bytes of image", len(data))
    return {"text": text}


@app.get("/events")
async def events(request: Request):
    """Server-sent events: reminders push through here.

    One queue per client (see EventHub); the finally block unsubscribes when
    the client goes away, which Starlette signals by closing the generator.
    """

    async def generate():
        queue = event_hub.subscribe()
        try:
            # Let the frontend distinguish "connected" from "still dialling".
            yield sse_event({"type": "connected"})
            # Someone is listening again: deliver anything that came due while
            # the screen was off, without waiting up to a full poll interval.
            # Guarded so a transient store error churns the reconnect instead
            # of killing the stream outright.
            try:
                # asyncio.to_thread cannot be interrupted: the scan runs to
                # completion on its worker thread even if the client
                # disconnects now, so rows it claims are marked fired with no
                # one left to deliver them. Shield the claim, and on
                # cancellation re-arm whatever was claimed before re-raising.
                claim_task = asyncio.create_task(
                    asyncio.to_thread(reminder_service.check_reminders)
                )
                events, power_actions = await asyncio.shield(claim_task)
                for fired in events:
                    event_hub.publish(reminder_event(fired))
                    store.mark_delivered(fired["id"])
                # Rows claimed here are marked fired and will not be picked up
                # by the poll loop — execute them now, exactly as that loop
                # would, so a scheduled shutdown due during downtime actually
                # happens instead of being silently dropped.
                for row in power_actions:
                    await reminder_service._run_power_action(row)
            except asyncio.CancelledError:
                # The client went away mid-claim. The thread already finished
                # and marked the rows fired; re-arm them so the next listener
                # or the poll loop delivers them instead of losing them.
                try:
                    if not claim_task.done():
                        await asyncio.shield(claim_task)
                    events, power_actions = claim_task.result()
                except Exception:
                    events, power_actions = [], []
                for reminder in events + power_actions:
                    if store.unmark_fired(reminder["id"]):
                        logger.warning(
                            "Re-armed reminder %d after disconnect", reminder["id"]
                        )
                raise
            except Exception as e:
                logger.warning("Reminder check on connect failed: %s", e, exc_info=True)
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
