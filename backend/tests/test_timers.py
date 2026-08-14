"""TimerService with a fake monotonic clock and a no-op loop sleep: firing is
deterministic instead of waiting real seconds."""

import asyncio
import contextlib

import pytest

from app.services import timers as timers_mod
from app.services.events import event_hub


class FakeMonotonic:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTime:
    """A stand-in `time` module for timers.py.

    Replacing the whole module (rather than patching time.monotonic globally)
    matters: asyncio's event loop reads the real time.monotonic, and freezing
    it would freeze the loop's own scheduling — wait_for timeouts would never
    fire and the suite would hang.
    """

    monotonic = FakeMonotonic()


@pytest.fixture
def fake_clock(monkeypatch):
    # Tick the loop in real time instead of waiting a full second per tick;
    # TICK_SECONDS is a module-level value read inside _loop.
    monkeypatch.setattr(timers_mod, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(timers_mod, "time", FakeTime())
    return FakeTime.monotonic


async def test_add_clamps_bounds(fake_clock):
    svc = timers_mod.TimerService()
    low = svc.add(0, "zero")
    high = svc.add(500, "huge")
    assert low["minutes"] == 1
    assert high["minutes"] == timers_mod.MAX_TIMER_MINUTES


def test_add_defaults_label(fake_clock):
    svc = timers_mod.TimerService()
    timer = svc.add(3)
    assert timer["text"] == "3-minute timer"


def test_cancel_by_text_either_direction(fake_clock):
    svc = timers_mod.TimerService()
    svc.add(10, "pasta")
    assert svc.cancel_by_text("pasta")[0] == 1
    assert svc.active() == []

    svc.add(10, "pasta")
    # Needle can also contain the whole timer text.
    assert svc.cancel_by_text("10-minute pasta timer") is not None
    assert svc.active() == []


def test_cancel_unknown_returns_none(fake_clock):
    svc = timers_mod.TimerService()
    svc.add(10, "pasta")
    assert svc.cancel_by_text("biryani") is None
    assert len(svc.active()) == 1


async def test_timer_fires_and_publishes(fake_clock):
    queue = event_hub.subscribe()
    try:
        svc = timers_mod.TimerService()
        svc.add(2, "eggs")
        fake_clock.advance(121)  # past end_at

        task = asyncio.create_task(svc._loop())
        try:
            event = await asyncio.wait_for(queue.get(), 2)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert event["type"] == "timer"
        assert "eggs" in event["text"]
        assert event["emotion"] == "happy"
        assert svc.active() == []
    finally:
        event_hub.unsubscribe(queue)


async def test_timer_does_not_fire_early(fake_clock):
    queue = event_hub.subscribe()
    try:
        svc = timers_mod.TimerService()
        svc.add(5, "pasta")
        fake_clock.advance(299)  # still inside the 5 minutes

        task = asyncio.create_task(svc._loop())
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(queue.get(), 0.2)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert len(svc.active()) == 1
    finally:
        event_hub.unsubscribe(queue)