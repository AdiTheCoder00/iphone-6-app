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
# noise, not a reminder. Past this, it is retired silently — though a
# recurring reminder keeps its schedule: the late occurrence is skipped, the
# next occurrence is re-armed.
MAX_LATE_SECONDS = 6 * 3600

# A scheduled sleep/shutdown/lock that keeps failing must not hammer the
# machine every poll tick forever. Each failure re-arms the row at a growing
# delay (1m, 2m, 4m, ... capped at 30m), so a persistently failing action
# retries at most every 30 minutes rather than every 30 seconds.
POWER_RETRY_INITIAL_SECONDS = 60
POWER_RETRY_MAX_SECONDS = 30 * 60
# Retry counts live in a per-reminder in-memory dict: restarting resets the
# backoff to 1m, which is fine for a desk device. The dict is capped so long
# chains of failed rows cannot grow it without bound.
_MAX_TRACKED_POWER_RETRIES = 64
_power_retries: dict[int, int] = {}


def _next_power_retry_delay(reminder_id: int) -> float:
    """Backoff for the next attempt of a failing power row."""
    attempts = _power_retries.get(reminder_id, 0)
    _power_retries[reminder_id] = attempts + 1
    if len(_power_retries) > _MAX_TRACKED_POWER_RETRIES:
        _power_retries.pop(next(iter(_power_retries)), None)
    return min(POWER_RETRY_INITIAL_SECONDS * (2**attempts), POWER_RETRY_MAX_SECONDS)


def reminder_event(reminder: dict) -> dict:
    """SSE payload for a fired reminder. Kept in one place so the poller and
    the SSE connect path publish identical events."""
    return {
        "type": "reminder",
        "id": reminder["id"],
        "text": reminder["text"],
        "emotion": "happy",
    }


class ReminderService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def add(
        self,
        text: str,
        minutes_from_now: int,
        repeat: str | None = None,
        power_action: str | None = None,
    ) -> dict:
        fire_at = datetime.now() + timedelta(minutes=minutes_from_now)
        reminder = store.add_reminder(text, fire_at.timestamp(), repeat=repeat, power_action=power_action)
        logger.info(
            "Reminder %d set for %s%s: %s",
            reminder["id"],
            fire_at,
            f" (repeats {repeat})" if repeat else "",
            text,
        )
        return {**reminder, "fire_time_dt": fire_at}

    def add_at(self, text: str, fire_time: float, repeat: str | None = None) -> dict:
        """Absolute-time reminder, for 'at 7pm' and recurring clock times."""
        reminder = store.add_reminder(text, fire_time, repeat=repeat)
        logger.info(
            "Reminder %d set for %s%s: %s",
            reminder["id"],
            datetime.fromtimestamp(fire_time),
            f" (repeats {repeat})" if repeat else "",
            text,
        )
        return {**reminder, "fire_time_dt": datetime.fromtimestamp(fire_time)}

    def pending(self) -> list[dict]:
        return store.pending_reminders()

    def reconcile(self) -> int:
        """Re-arm reminders claimed by a process that died mid-delivery.

        mark_fired then publish is a few instructions wide, but a hard crash
        in that window (or during the SSE connect path's claim) would
        otherwise leave the row fired with no delivered flag and nothing ever
        send it again. Called once at startup, before the poller starts, so
        every re-armed row is picked up by the next scan.
        """
        rearmed = 0
        for row in store.stale_claimed():
            if store.rearm_reminder(row["id"]):
                rearmed += 1
                logger.warning(
                    "Re-armed reminder %d lost to a crash before delivery: %s",
                    row["id"],
                    row["text"],
                )
        if rearmed:
            logger.info("Startup reconciliation re-armed %d reminder(s)", rearmed)
        return rearmed

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

    def check_reminders(self) -> tuple[list[dict], list[dict]]:
        """Internal, NOT model-facing. Claim (mark fired) everything now due
        and return the claimed rows; callers publish them to the SSE hub from
        the event loop.

        Returns (events, power_actions). Power rows (scheduled shutdown/sleep/
        lock) are claimed and returned even with nobody listening — a scheduled
        shutdown must not wait for an SSE client — while plain reminders stay
        pending until a listener exists, so nothing is lost.

        Publishing and claiming are deliberately split: check_reminders runs
        in a worker thread, and asyncio.Queue (the hub's internals) is not
        thread-safe. Returning the claims keeps every publish on the loop
        thread, and the caller publishes immediately after the claim returns,
        so the window in which a claim could be lost to a disconnect is a few
        instructions rather than the whole scan.
        """
        now = time.time()
        due = store.due_reminders(now)
        if not due:
            return [], []

        # Retire anything hopelessly stale regardless of who is listening.
        fresh, power = [], []
        for reminder in due:
            if now - reminder["fire_time"] > MAX_LATE_SECONDS:
                if store.mark_fired(reminder["id"]):
                    if reminder.get("repeat"):
                        # Skip this late occurrence, but keep the schedule:
                        # retiring the row would silently kill a daily/weekly
                        # reminder that merely happened to fall inside a long
                        # outage.
                        store.rearm_recurring(
                            reminder["id"], reminder["repeat"], reminder["fire_time"]
                        )
                        logger.info(
                            "Reminder %d skipped unsent (%.1fh late), recurrence kept (%s): %s",
                            reminder["id"],
                            (now - reminder["fire_time"]) / 3600.0,
                            reminder["repeat"],
                            reminder["text"],
                        )
                    else:
                        # Terminal, like delivery: this row must never be picked up
                        # by the startup reconciliation and re-fired.
                        store.mark_delivered(reminder["id"])
                        logger.info(
                            "Reminder %d retired unsent (%.1fh late): %s",
                            reminder["id"],
                            (now - reminder["fire_time"]) / 3600.0,
                            reminder["text"],
                        )
            elif reminder.get("power_action"):
                power.append(reminder)
            else:
                fresh.append(reminder)

        # No listener: leave plain reminders pending and try again next tick.
        # (Reading subscriber_count from a worker thread races harmlessly — it
        # only decides whether to claim now or later.)
        if not fresh or event_hub.subscriber_count == 0:
            if fresh:
                logger.info("%d reminder(s) due but nobody connected; holding", len(fresh))
            fresh = []

        fired = []
        try:
            for reminder in fresh + power:
                # Claim before returning: if the claim loses a race, another
                # caller already sent this one and we must not send it twice.
                if store.mark_fired(reminder["id"]):
                    if reminder.get("repeat"):
                        store.rearm_recurring(
                            reminder["id"], reminder["repeat"], reminder["fire_time"]
                        )
                        logger.info(
                            "Reminder %d fired and re-armed (%s): %s",
                            reminder["id"],
                            reminder["repeat"],
                            reminder["text"],
                        )
                    fired.append(reminder)
        except Exception as e:
            # A mid-scan failure (DB lock, disk error) must not leave the rows
            # claimed so far marked fired forever with nobody to deliver them:
            # re-arm everything claimed in this scan, then let the error
            # propagate so the poll loop can log it and try again next tick.
            for reminder in fired:
                if store.unmark_fired(reminder["id"]):
                    logger.warning(
                        "Re-armed reminder %d after failed claim scan: %s",
                        reminder["id"],
                        e,
                    )
            raise

        events = [r for r in fired if not r.get("power_action")]
        power_actions = [r for r in fired if r.get("power_action")]
        return events, power_actions

    async def _run_power_action(self, row: dict) -> None:
        """Execute a claimed power row: scheduled sleep/shutdown/lock."""
        from app.config import settings
        from app.services import pc_control

        action = row.get("power_action")

        def _act() -> None:
            if action == "sleep":
                pc_control.sleep_pc()
            elif action == "shutdown":
                pc_control.shutdown_pc(settings.pc_shutdown_delay_seconds)
            elif action == "lock":
                pc_control.lock_screen()
            else:
                raise ValueError(f"unknown power_action {action!r}")

        try:
            await asyncio.to_thread(_act)
            _power_retries.pop(row["id"], None)
            logger.info("Scheduled power action %r executed: %s", action, row["text"])
        except Exception as e:
            logger.error("Scheduled power action %r failed: %s", action, e)
            # Re-arm the claim so the next poll retries — a failed scheduled
            # shutdown must not be silently lost. Guarded on fired = 1, so
            # only the claimer can re-arm; the growing delay bounds how often
            # a persistently failing action keeps retrying (1m..30m) instead
            # of hammering the machine every poll tick.
            delay = _next_power_retry_delay(row["id"])
            if store.unmark_fired(row["id"], retry_at=time.time() + delay):
                logger.warning(
                    "Power action %r re-armed for retry in %.0fs (reminder %d)",
                    action,
                    delay,
                    row["id"],
                )

    async def _poll_loop(self) -> None:
        # Set as soon as the events have been handed to subscribers. The
        # publish loop is synchronous (no await points), so once the power
        # actions are being run every event has been delivered; the flag
        # tells the cancellation handler exactly that.
        published: set[int] = set()
        # Power rows whose action has been handed to a thread: asyncio.to_thread
        # runs to completion even when the awaiting coroutine is cancelled, so
        # an id in here has actually executed and must never be re-armed.
        executed: set[int] = set()
        claim_task: asyncio.Task | None = None
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                # Claims happen in the thread; publishing stays on the loop.
                # The claim is shielded: to_thread cannot be interrupted, so
                # a shutdown arriving mid-claim would leave rows marked fired
                # and never delivered. On cancellation, re-arm those claims.
                claim_task = asyncio.create_task(
                    asyncio.to_thread(self.check_reminders)
                )
                events, power_actions = await asyncio.shield(claim_task)
                # A listener can only vanish at an await point, so the check,
                # every publish and the bookkeeping below stay in one
                # synchronous stretch: once the check reads listeners > 0, no
                # disconnect can land before every event is in a live queue.
                # Events claimed by a thread that saw a listener can still hit
                # zero listeners by now — re-arm those rather than marking
                # them delivered into the void.
                if events and event_hub.subscriber_count == 0:
                    for fired in events:
                        if store.unmark_fired(fired["id"]):
                            logger.info(
                                "Reminder %d re-armed (listener left before delivery)",
                                fired["id"],
                            )
                    events = []
                published = set()
                for fired in events:
                    event_hub.publish(reminder_event(fired))
                    # Set as soon as the events have been handed to
                    # subscribers. The publish block is synchronous (no await
                    # points), so once the power actions are being run every
                    # event has been delivered; the flag tells the
                    # cancellation handler exactly that.
                    published.add(fired["id"])
                for fired in events:
                    await asyncio.to_thread(store.mark_delivered, fired["id"])
                    logger.info("Reminder %d fired: %s", fired["id"], fired["text"])
                executed = set()
                for row in power_actions:
                    # Recorded before the await: to_thread completes the
                    # action even if this loop is cancelled mid-await, so the
                    # cancellation handler must treat the row as done.
                    executed.add(row["id"])
                    await self._run_power_action(row)
            except asyncio.CancelledError:
                try:
                    if claim_task is not None and not claim_task.done():
                        await asyncio.shield(claim_task)
                    events, power_actions = (
                        claim_task.result() if claim_task is not None else ([], [])
                    )
                except Exception:
                    events, power_actions = [], []
                for reminder in events + power_actions:
                    # Events already handed to subscribers ARE the delivery,
                    # and power actions already handed to a thread HAVE run —
                    # re-arming either would just fire them again next boot.
                    # Anything else was claimed but never handled: re-arm so
                    # the next listener or the poll loop delivers it.
                    if reminder["id"] in published or reminder["id"] in executed:
                        continue
                    if await asyncio.to_thread(store.unmark_fired, reminder["id"]):
                        logger.warning(
                            "Re-armed reminder %d after shutdown", reminder["id"]
                        )
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
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("Reminder service stopped with error: %s", e)
            self._task = None


reminder_service = ReminderService()
