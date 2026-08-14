"""Proactive presence — the companion speaking first.

Everything else in this app is request/response. This is the one place the
device initiates, which is most of what separates a companion from a chatbot
with a face.

Three rules govern every push, because an ambient device that talks when it
shouldn't gets unplugged:

  1. Never speak to an empty room. A push only happens when at least one SSE
     client is connected — that is the closest available proxy for "the screen
     is on and someone could see it".
  2. Never speak during quiet hours.
  3. Never speak twice about the same thing. Each trigger records when it last
     fired and will not repeat inside its own window.

Lines are improvised by the model so they are not identical every morning,
with a canned fallback when Ollama is down — a silent companion is better
than an error bubble.
"""

import asyncio
import logging
import random
import time
from datetime import datetime

from app.config import settings
from app.services.companion import companion_service
from app.services.events import event_hub

logger = logging.getLogger(__name__)

# One minute is fine: every trigger is hour-scale, so a tighter loop would
# only burn cycles.
TICK_SECONDS = 60

_FALLBACK_MORNING = [
    "morning. hope you slept alright.",
    "good morning — here when you need me.",
    "morning. the desk missed you.",
]
_FALLBACK_RAIN = [
    "looks like rain today — maybe grab an umbrella on the way out.",
    "rain's in the forecast. the plants might appreciate it.",
]
_FALLBACK_BATTERY = [
    "your battery is getting low — worth plugging in?",
    "battery's running down. a charge now saves hunting for a socket later.",
]
_FALLBACK_SESSION = [
    "you've been at it a while. worth a stretch?",
    "long session. maybe get some water?",
]
_FALLBACK_IDLE = [
    "still here if you need me.",
    "quiet one today. i'm around.",
]


class ProactiveService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        # Monotonic timestamps of the last push per trigger.
        self._last_morning_date: str | None = None
        self._last_rain_date: str | None = None
        self._last_battery_nudge = 0.0
        self._last_session_nudge = 0.0
        self._last_idle_nudge = 0.0
        # Set by the chat route on every user message, so "idle" means idle
        # from the user's side, not merely no pushes.
        self._last_user_activity = time.monotonic()
        # When the first SSE client of the current stretch connected.
        self._session_started: float | None = None

    def note_user_activity(self) -> None:
        self._last_user_activity = time.monotonic()
        # A message also ends the current "long unbroken session" clock: they
        # are clearly present and engaged, so the stretch nudge restarts.
        self._session_started = time.monotonic()

    def _in_quiet_hours(self, now: datetime) -> bool:
        start = settings.proactive_quiet_start
        end = settings.proactive_quiet_end
        hour = now.hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        # Window wraps midnight (the normal case: 22 -> 7).
        return hour >= start or hour < end

    async def _push(self, instruction: str, fallback: list[str], default_emotion: str) -> None:
        try:
            result = await companion_service.improvise(instruction)
            text, emotion = result["reply"], result["emotion"]
        except Exception as e:
            logger.info("Proactive line fell back to canned (%s)", e)
            text, emotion = random.choice(fallback), default_emotion

        event_hub.publish({"type": "proactive", "text": text, "emotion": emotion})
        logger.info("Proactive push: %r (%s)", text, emotion)

    async def _morning_instruction(self) -> str:
        """Morning greeting with today's briefing folded in.

        The briefing assembly never raises and needs no Ollama, so even when
        it is empty (no weather city configured) the greeting itself survives.
        """
        briefing = ""
        try:
            from app.services import tools as tools_module

            briefing = await tools_module._get_briefing()
        except Exception as e:
            logger.info("Morning briefing unavailable: %s", e)
        if not briefing:
            return "Greet the user good morning in your own words. One short sentence."
        return (
            "Greet the user good morning in your own words, then a very short digest. "
            f"Only include the parts below that say something: {briefing} "
            "Two short sentences at most, warm and plain."
        )

    async def _tick(self) -> None:
        if not settings.proactive_enabled:
            return

        # Rule 1: nobody is listening.
        if event_hub.subscriber_count == 0:
            self._session_started = None
            return
        if self._session_started is None:
            self._session_started = time.monotonic()

        now = datetime.now()
        # Rule 2.
        if self._in_quiet_hours(now):
            return

        mono = time.monotonic()

        # --- morning greeting: first tick past the hour, once per day
        today = now.strftime("%Y-%m-%d")
        if (
            self._last_morning_date != today
            and now.hour == settings.proactive_morning_hour
        ):
            self._last_morning_date = today
            await self._push(self._morning_instruction(), _FALLBACK_MORNING, "happy")
            return

        # --- rain alert: once per day when today's forecast says rain
        # Checking once per day is the point; whether the check succeeds, mark
        # the date so a flaky feed does not retry every minute.
        if (
            settings.rain_alert_threshold > 0
            and settings.weather_city
            and self._last_rain_date != today
        ):
            self._last_rain_date = today
            try:
                from app.services import tools as tools_module

                forecast = await tools_module.fetch_forecast(settings.weather_city)
                prob = forecast.get("today_precip_prob") or 0
                if prob >= settings.rain_alert_threshold:
                    await self._push(
                        f"Rain is likely in {forecast['label']} today "
                        f"({prob:.0f}% chance). Mention it in one short sentence, "
                        "and suggest an umbrella if it fits the tone.",
                        _FALLBACK_RAIN,
                        "happy",
                    )
                    return
            except Exception as e:
                logger.info("Rain check skipped: %s", e)

        # --- battery-low nudge (laptops only; desktops have no battery)
        if (
            settings.battery_alert_threshold > 0
            and mono - self._last_battery_nudge
            >= settings.battery_alert_interval_hours * 3600.0
        ):
            self._last_battery_nudge = mono
            try:
                from app.services import pc_control

                # system_stats() samples CPU over a blocking 0.3s window; the
                # proactive loop must not stall SSE keepalives for it.
                stats = await asyncio.to_thread(pc_control.system_stats)
                percent = stats.get("battery_percent")
                if percent is not None and percent <= settings.battery_alert_threshold:
                    await self._push(
                        f"The user's laptop battery is at {percent}%. Nudge them to "
                        "plug in, in one short warm sentence.",
                        _FALLBACK_BATTERY,
                        "happy",
                    )
                    return
            except Exception as e:
                logger.info("Battery check skipped: %s", e)

        # --- long unbroken session
        session_hours = (mono - self._session_started) / 3600.0
        if (
            settings.proactive_session_hours > 0
            and session_hours >= settings.proactive_session_hours
            and mono - self._last_session_nudge >= settings.proactive_session_hours * 3600.0
        ):
            self._last_session_nudge = mono
            await self._push(
                "The user has been at their desk for hours without a break. "
                "Nudge them to stretch or drink water, warmly, in one short sentence.",
                _FALLBACK_SESSION,
                "happy",
            )
            return

        # --- idle check-in
        idle_hours = (mono - self._last_user_activity) / 3600.0
        if (
            settings.proactive_idle_hours > 0
            and idle_hours >= settings.proactive_idle_hours
            and mono - self._last_idle_nudge >= settings.proactive_idle_hours * 3600.0
        ):
            self._last_idle_nudge = mono
            await self._push(
                "The user has not spoken to you in hours. Say something brief and "
                "unobtrusive to let them know you're still here. One short sentence.",
                _FALLBACK_IDLE,
                "idle",
            )

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(TICK_SECONDS)
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Proactive tick failed: %s", e, exc_info=True)

    def start(self) -> None:
        if not settings.proactive_enabled:
            logger.info("Proactive presence disabled")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info(
                "Proactive presence started (morning=%02d:00, quiet=%02d:00-%02d:00)",
                settings.proactive_morning_hour,
                settings.proactive_quiet_start,
                settings.proactive_quiet_end,
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


proactive_service = ProactiveService()
