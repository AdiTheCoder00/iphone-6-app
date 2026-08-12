"""Reminder scheduling. State lives in SQLite (see services/store.py).

The poller reads due rows rather than holding a schedule in memory, so a
restart loses nothing: reminders set before the restart still fire, and one
set for a time that passed while the server was down fires immediately on the
next tick rather than being silently skipped.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta

from app.services.events import event_hub
from app.services.store import store

logger = logging.getLogger(__name__)

# Reminders are minute-granularity, so 30s bounds worst-case lateness at half
# a minute.
POLL_INTERVAL_SECONDS = 30

# A fired reminder is delivered over SSE and nothing else, so firing one with
# no client connected would mark it done and lose it forever. Instead it stays
# pending until someone is listening — better late than never.
#
# But only up to a point: a reminder that surfaces days after the fact is
# noise, not a reminder. Past this, it is retired silently.
MAX_LATE_SECONDS = 6 * 3600


class ReminderService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def add(self, text: str, minutes_from_now: int) -> dict:
        fire_at = datetime.now() + timedelta(minutes=minutes_from_now)
        reminder = store.add_reminder(text, fire_at.timestamp())
        logger.info("Reminder %d set for %s: %s", reminder["id"], fire_at, text)
        return {**reminder, "fire_time_dt": fire_at}

    def pending(self) -> list[dict]:
        return store.pending_reminders()

    def cancel(self, reminder_id: int) -> bool:
        cancelled = store.delete_reminder(reminder_id)
        if cancelled:
            logger.info("Reminder %d cancelled", reminder_id)
        return cancelled

    def snooze(self, reminder_id: int, minutes: int) -> dict | None:
        fire_time = time.time() + timedelta(minutes=minutes).total_seconds()
        reminder = store.snooze_fired_reminder(reminder_id, fire_time)
        if reminder:
            logger.info("Reminder %d snoozed for %d minute(s)", reminder_id, minutes)
        return reminder

    def find_pending(self, text: str) -> list[dict]:
        """Pending reminders whose text loosely matches `text`.

        Loose on purpose: the user says "cancel the plant one", not the exact
        wording the model stored. Substring matching in either direction
        catches both "plants" against "Water the plants" and the reverse.
        """
        needle = (text or "").strip().lower()
        if not needle:
            return []
        matches = []
        for reminder in store.pending_reminders():
            haystack = reminder["text"].lower()
            if needle in haystack or haystack in needle:
                matches.append(reminder)
        return matches

    def check_reminders(self) -> list[dict]:
        """Internal, NOT model-facing. Fire everything now due."""
        now = time.time()
        due = store.due_reminders(now)
        if not due:
            return []

        # Retire anything hopelessly stale regardless of who is listening.
        fresh = []
        for reminder in due:
            if now - reminder["fire_time"] > MAX_LATE_SECONDS:
                if store.mark_fired(reminder["id"]):
                    logger.info(
                        "Reminder %d retired unsent (%.1fh late): %s",
                        reminder["id"],
                        (now - reminder["fire_time"]) / 3600.0,
                        reminder["text"],
                    )
            else:
                fresh.append(reminder)

        # No listener: leave them pending and try again next tick.
        if not fresh or event_hub.subscriber_count == 0:
            if fresh:
                logger.info("%d reminder(s) due but nobody connected; holding", len(fresh))
            return []

        fired = []
        for reminder in fresh:
            # Claim before publishing: if the claim loses a race, another
            # caller already sent this one and we must not send it twice.
            if not store.mark_fired(reminder["id"]):
                continue
            event_hub.publish(
                {
                    "type": "reminder",
                    "id": reminder["id"],
                    "text": reminder["text"],
                    "emotion": "happy",
                }
            )
            logger.info("Reminder %d fired: %s", reminder["id"], reminder["text"])
            fired.append(reminder)
        return fired

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                await asyncio.to_thread(self.check_reminders)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # One bad reminder must not kill the loop for all the others.
                logger.error("Reminder poll failed: %s", e, exc_info=True)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())
            outstanding = len(self.pending())
            logger.info(
                "Reminder poller started (every %ds, %d pending from previous runs)",
                POLL_INTERVAL_SECONDS,
                outstanding,
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


reminder_service = ReminderService()
