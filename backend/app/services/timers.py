"""Countdown timers.

Reminders are datetime-anchored and minute-granular; timers are the kitchen
timer: always relative to now, one-shot, and usually short. They live in
memory only — a backend restart clears them, which is fine for a desk device:
the phone reconnects in seconds, and nothing here is worth surviving a
restart.

Firing uses the same SSE hub as reminders: the phone renders a "timer" event
like a reminder and speaks it.
"""

import asyncio
import itertools
import logging
import time

from app.services.events import event_hub

logger = logging.getLogger(__name__)

# Timers need second-level precision, so unlike the 30s reminder poller this
# loop ticks every second. A one-shot timers list keeps the cost trivial.
TICK_SECONDS = 1.0

# Bounds what the model can ask for; also bounds the in-memory lifetime.
MAX_TIMER_MINUTES = 180


def timer_event(timer: dict) -> dict:
    """SSE payload for a fired timer, kept in one place like reminder_event."""
    return {
        "type": "timer",
        "id": timer["id"],
        "text": f"Timer done — {timer['text']}.",
        "emotion": "happy",
    }


class TimerService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        # id -> {"id", "end_at" (monotonic), "minutes", "text"}
        self._timers: dict[int, dict] = {}
        self._next_id = itertools.count(1)

    def add(self, minutes: int, label: str = "") -> dict:
        minutes = max(1, min(int(minutes), MAX_TIMER_MINUTES))
        label = (label or "").strip()
        text = label or f"{minutes}-minute timer"
        timer_id = next(self._next_id)
        self._timers[timer_id] = {
            "id": timer_id,
            "end_at": time.monotonic() + minutes * 60,
            "minutes": minutes,
            "text": text,
        }
        logger.info("Timer %d set for %d minute(s)", timer_id, minutes)
        return self._timers[timer_id]

    def cancel_by_text(self, text: str) -> tuple[int, str] | None:
        """Loose match like the reminder lookup: substring in either direction,
        so "pasta" cancels "10-minute pasta timer" and vice versa."""
        needle = (text or "").strip().lower()
        if not needle:
            return None
        for timer in list(self._timers.values()):
            haystack = timer["text"].lower()
            if needle in haystack or haystack in needle:
                self._timers.pop(timer["id"], None)
                logger.info("Timer %d cancelled", timer["id"])
                return timer["id"], timer["text"]
        return None

    def active(self) -> list[dict]:
        return [dict(t) for t in self._timers.values()]

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(TICK_SECONDS)
                now = time.monotonic()
                due = [t for t in self._timers.values() if now >= t["end_at"]]
                for timer in due:
                    self._timers.pop(timer["id"], None)
                    event_hub.publish(timer_event(timer))
                    logger.info("Timer %d fired", timer["id"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # One bad publish must not kill the timer service for the rest
                # of the session — a timed-out bucket of water matters more
                # than the log line that would have documented the failure.
                logger.error("Timer tick failed: %s", e, exc_info=True)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info("Timer service started (tick every %gs)", TICK_SECONDS)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                # Cancellation is expected; anything else is a real defect and
                # must be visible rather than swallowed.
                logger.error("Timer service stopped with error: %s", e)
            self._task = None


timer_service = TimerService()