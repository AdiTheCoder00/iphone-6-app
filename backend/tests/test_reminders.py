"""ReminderService claim/publish semantics — especially the power-action rows
that must fire even with zero SSE listeners.

The publish path lives in _poll_loop (check_reminders only claims); those tests
drive the real loop with a no-op sleep so a fired reminder lands on the queue
without waiting POLL_INTERVAL_SECONDS. Power-row execution (_run_power_action)
is stubbed — it would sleep/shut down the test machine for real.
"""

import asyncio
import contextlib
import time

import pytest

from app.services import reminders as reminders_mod
from app.services.events import event_hub
from app.services.reminders import reminder_service
from app.services.store import store


async def _noop_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


async def _direct_call(fn, *args, **kwargs):
    return fn(*args, **kwargs)


@pytest.fixture
def poller(monkeypatch):
    """Run the poll loop with a fast tick and no real thread hops.

    POLL_INTERVAL_SECONDS is a module-level value read inside _poll_loop, so
    shortening it never touches the global asyncio.sleep. The to_thread stub
    must return an awaitable — the loop awaits its result. _run_power_action
    is stubbed — it would sleep/shut down the test machine for real.
    """
    monkeypatch.setattr(reminders_mod, "POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(reminders_mod.asyncio, "to_thread", _direct_call)
    monkeypatch.setattr(reminder_service, "_run_power_action", _noop_sleep)


async def _drive_poll(task: asyncio.Task, queue: asyncio.Queue, timeout: float = 2.0) -> dict:
    try:
        return await asyncio.wait_for(queue.get(), timeout)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_fires_and_publishes_with_listener(fresh_store, no_subscribers, poller):
    queue = event_hub.subscribe()
    try:
        row = reminder_service.add("call mom", 0)
        task = asyncio.create_task(reminder_service._poll_loop())
        event = await _drive_poll(task, queue)
        assert event["type"] == "reminder" and event["id"] == row["id"]
        assert reminder_service.pending() == []  # claimed, not re-listed
    finally:
        event_hub.unsubscribe(queue)


def test_held_without_listener(fresh_store, no_subscribers):
    row = reminder_service.add("call mom", 0)
    events, power = reminder_service.check_reminders()
    assert events == [] and power == []
    pending = reminder_service.pending()
    assert [r["id"] for r in pending] == [row["id"]]  # still pending


def test_power_row_claimed_without_listener(fresh_store, no_subscribers):
    row = reminder_service.add("shutdown at midnight", 0, power_action="shutdown")
    events, power = reminder_service.check_reminders()
    assert events == []
    assert [r["id"] for r in power] == [row["id"]]
    assert reminder_service.pending() == []  # claimed even with nobody listening


async def test_recurring_rearms_after_fire(fresh_store, no_subscribers, poller):
    queue = event_hub.subscribe()
    try:
        past = time.time() - 60
        row = reminder_service.add_at("water plants", past, repeat="daily")
        task = asyncio.create_task(reminder_service._poll_loop())
        event = await _drive_poll(task, queue)
        assert event["type"] == "reminder" and event["id"] == row["id"]

        after = store.pending_reminders()
        assert len(after) == 1
        assert after[0]["repeat"] == "daily"
        assert after[0]["fire_time"] == pytest.approx(row["fire_time"] + 86400)
    finally:
        event_hub.unsubscribe(queue)


def test_stale_retired_silently(fresh_store, no_subscribers):
    reminder_service.add_at("ancient", time.time() - 7 * 3600)
    events, power = reminder_service.check_reminders()
    assert events == [] and power == []
    assert reminder_service.pending() == []


async def test_snooze_moves_fired_reminder(fresh_store, no_subscribers, poller):
    queue = event_hub.subscribe()
    try:
        row = reminder_service.add("water plants", 0)
        task = asyncio.create_task(reminder_service._poll_loop())
        await _drive_poll(task, queue)
        assert reminder_service.pending() == []

        moved = reminder_service.snooze(row["id"], 30)
        assert moved is not None
        assert moved["fire_time"] > time.time() + 29 * 60
    finally:
        event_hub.unsubscribe(queue)


def test_snooze_refuses_unfired(fresh_store, no_subscribers):
    row = reminder_service.add("water plants", 30)
    assert reminder_service.snooze(row["id"], 30) is None