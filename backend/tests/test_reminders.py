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


@pytest.fixture(autouse=True)
def clean_power_retries():
    """_power_retries is module-level: a test must not inherit another test's
    backoff counts."""
    reminders_mod._power_retries.clear()
    yield
    reminders_mod._power_retries.clear()


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


def test_stale_recurring_skipped_but_schedule_kept(fresh_store, no_subscribers):
    """A recurring reminder that missed its slot by >MAX_LATE_SECONDS must not
    lose the recurrence: the late occurrence is skipped, the next one stays."""
    past = time.time() - 7 * 3600
    row = reminder_service.add_at("water plants", past, repeat="daily")
    events, power = reminder_service.check_reminders()
    assert events == [] and power == []
    after = store.pending_reminders()
    assert len(after) == 1
    assert after[0]["repeat"] == "daily"
    step = after[0]["fire_time"] - row["fire_time"]
    assert 82800 < step < 90000  # ~24h, tolerant of a DST shift


async def test_power_row_not_rearmed_after_poll_cancel(
    fresh_store, no_subscribers, monkeypatch
):
    """A shutdown that already executed must not be re-armed when the poll loop
    is cancelled: the next boot would otherwise run it a second time."""
    monkeypatch.setattr(reminders_mod, "POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(reminders_mod.asyncio, "to_thread", _direct_call)
    ran: list[int] = []

    async def fake_run(row):
        ran.append(row["id"])
        await asyncio.sleep(0)

    monkeypatch.setattr(reminder_service, "_run_power_action", fake_run)
    row = reminder_service.add("shutdown at midnight", 0, power_action="shutdown")

    task = asyncio.create_task(reminder_service._poll_loop())
    for _ in range(200):
        if ran:
            break
        await asyncio.sleep(0.01)
    assert ran == [row["id"]]  # the action ran before the cancel landed
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert reminder_service.pending() == []  # claimed and executed, not re-armed
    assert store.due_reminders() == []


async def test_power_failure_rearms_with_growing_backoff(
    fresh_store, no_subscribers, monkeypatch
):
    """A failing power action retries at a growing delay (1m, 2m, ...) instead
    of every poll tick, and success clears the retry state."""

    def boom():
        raise RuntimeError("boom")

    from app.services import pc_control

    monkeypatch.setattr(pc_control, "shutdown_pc", boom)
    row = reminder_service.add("shutdown", 0, power_action="shutdown")

    events, power = reminder_service.check_reminders()
    assert power and not events
    await reminder_service._run_power_action(power[0])
    pending = store.pending_reminders()
    assert len(pending) == 1
    assert pending[0]["fire_time"] == pytest.approx(time.time() + 60, abs=5)

    # Second failure: the backoff doubles.
    conn = store._require()
    with store._lock:
        conn.execute(
            "UPDATE reminders SET fired = 0, fire_time = ? WHERE id = ?",
            (time.time() - 1, row["id"]),
        )
        conn.commit()
    events, power = reminder_service.check_reminders()
    assert power
    await reminder_service._run_power_action(power[0])
    pending = store.pending_reminders()
    assert len(pending) == 1
    assert pending[0]["fire_time"] == pytest.approx(time.time() + 120, abs=5)

    # Success clears the per-reminder retry state.
    monkeypatch.setattr(pc_control, "shutdown_pc", lambda delay: None)
    conn = store._require()
    with store._lock:
        conn.execute(
            "UPDATE reminders SET fired = 0, fire_time = ? WHERE id = ?",
            (time.time() - 1, row["id"]),
        )
        conn.commit()
    events, power = reminder_service.check_reminders()
    await reminder_service._run_power_action(power[0])
    assert reminders_mod._power_retries.get(row["id"]) is None


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


async def test_hub_rearms_reminder_when_queue_discarded(
    fresh_store, no_subscribers, monkeypatch
):
    """The disconnect-window loss: a reminder handed to a client's queue that
    is then thrown away before the client consumed it must be re-armed, or the
    event dies in the discarded queue and the row is marked delivered into the
    void. The re-arm task runs on the loop after unsubscribe."""
    monkeypatch.setattr(reminders_mod.asyncio, "to_thread", _direct_call)
    row = reminder_service.add("call mom", 0)
    queue = event_hub.subscribe()
    try:
        events, _ = reminder_service.check_reminders()  # claim: fired = 1
        event = reminders_mod.reminder_event(events[0])
        event_hub.publish(event)
        event_hub.unsubscribe(queue)  # client left before consuming
        await asyncio.sleep(0.05)  # let the re-arm task run
        pending = store.pending_reminders()
        assert [r["id"] for r in pending] == [row["id"]]
        # A mark_delivered landing after the re-arm is a no-op (fired = 0),
        # so the reminder fires again for the next listener instead of being
        # lost or double-marked.
        store.mark_delivered(row["id"])
        assert [r["id"] for r in store.pending_reminders()] == [row["id"]]
    finally:
        event_hub.unsubscribe(queue)


async def test_hub_ack_prevents_rearm(fresh_store, no_subscribers, monkeypatch):
    """Once a client actually consumed the reminder event, a later disconnect
    must not re-arm it: the reminder was delivered, and re-arming would make
    it fire a second time for the next listener."""
    monkeypatch.setattr(reminders_mod.asyncio, "to_thread", _direct_call)
    row = reminder_service.add("call mom", 0)
    queue = event_hub.subscribe()
    try:
        events, _ = reminder_service.check_reminders()  # claim: fired = 1
        event = reminders_mod.reminder_event(events[0])
        event_hub.publish(event)
        event_hub.ack(queue, event)  # consumed
        event_hub.unsubscribe(queue)
        await asyncio.sleep(0.05)
        # Still claimed-and-undelivered is the "no re-arm" signal: the row
        # must not come back as pending.
        assert store.pending_reminders() == []
    finally:
        event_hub.unsubscribe(queue)


def test_rearm_if_undelivered_refuses_delivered_rows(fresh_store, no_subscribers):
    """The delivered flag wins any race against the disconnect re-arm: a row
    already marked delivered must never fire again, or the reminder would
    duplicate for whoever is listening next."""
    queue = event_hub.subscribe()  # a listener is required for the claim
    try:
        row = reminder_service.add("call mom", 0)
        events, _ = reminder_service.check_reminders()
        assert [e["id"] for e in events] == [row["id"]]  # claimed: fired = 1
        store.mark_delivered(row["id"])  # delivered = 1
        assert store.rearm_if_undelivered(row["id"]) is False
        assert store.due_reminders() == []  # stays claimed-and-delivered
    finally:
        event_hub.unsubscribe(queue)