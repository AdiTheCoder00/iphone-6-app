"""In-process SSE fan-out.

One queue per connected client rather than a single shared queue: with a shared
queue the first reader to wake consumes the event and every other client misses
it. Queues are bounded so a client that stops reading (a backgrounded phone,
a paused tab) cannot grow memory without limit — it drops the oldest event
instead, which for reminders is the right trade.

Reminder events additionally carry delivery bookkeeping. A client can drop its
queue between "handed to the queue" and "consumed" — the publisher's
mark_delivered may already have committed by then, or may never run — and a
reminder that sat in a discarded queue is simply gone. publish() therefore
records which queues hold an unconsumed reminder event, ack() marks a copy as
consumed (the event is delivered once ANY client consumed it — every other
copy was a duplicate anyway), and unsubscribe() re-arms any event whose last
queue was thrown away without ever being consumed. The reminder row then
re-fires for the next listener instead of being lost.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Enough slack for a short background pause without buffering an unbounded
# backlog for a client that is never coming back.
MAX_QUEUED_EVENTS = 32

# Upper bound on delivery bookkeeping. Entries normally disappear on the
# first ack; they linger only while every client is stalled at once, so this
# cap is pure insurance against a pathological day of unread reminders.
MAX_TRACKED_PENDING = 2048


class EventHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        # Reminder event id -> queues that received a copy the client has not
        # consumed yet. publish() adds, ack() removes (delivery happened, the
        # other copies were duplicates), unsubscribe() removes and re-arms
        # ids whose set empties.
        self._pending: dict[int, set[asyncio.Queue]] = {}
        # Async callback (event id) fired when an event's last queue was
        # thrown away unconsumed; the reminder service uses it to re-arm the
        # row so it fires for the next listener.
        self._unclaimed: Callable[[int], Awaitable[None]] | None = None

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUED_EVENTS)
        self._subscribers.add(queue)
        logger.info("SSE client connected (%d total)", len(self._subscribers))
        return queue

    def set_unclaimed_handler(
        self, handler: Callable[[int], Awaitable[None]] | None
    ) -> None:
        self._unclaimed = handler

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)
        # The queue is being thrown away: anything still in it is
        # undeliverable. Drain it (consumed-without-ack and full-queue-drop
        # events are covered by the sweep below — they are no longer in the
        # queue but still hold it in their pending sets).
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        rearm: list[int] = []
        for event_id, queues in list(self._pending.items()):
            queues.discard(queue)
            if not queues:
                del self._pending[event_id]
                rearm.append(event_id)
        for event_id in rearm:
            self._schedule_unclaimed(event_id)
        logger.info("SSE client disconnected (%d remaining)", len(self._subscribers))

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: dict) -> None:
        """Fan an event out to every connected client. Never blocks."""
        live = list(self._subscribers)
        for queue in live:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest so a stalled client still gets recent events.
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    logger.warning("Dropped event for a stalled SSE client")
        if event.get("type") == "reminder" and live:
            event_id = event.get("id")
            if event_id is not None:
                if len(self._pending) >= MAX_TRACKED_PENDING:
                    # Oldest entries first (insertion order): stop tracking
                    # them rather than grow without bound. A disconnect then
                    # loses those reminders exactly as before the tracking
                    # existed — a bounded, documented fallback.
                    overflow = len(self._pending) - MAX_TRACKED_PENDING + 1
                    for stale in list(self._pending)[:overflow]:
                        del self._pending[stale]
                self._pending[event_id] = set(live)

    def ack(self, queue: asyncio.Queue, event: dict) -> None:
        """Called when a client has consumed an event from its queue.

        Any single consumption counts as delivery: every copy fanned out to
        the other clients was a duplicate anyway, so the bookkeeping is done.
        """
        if event.get("type") != "reminder":
            return
        event_id = event.get("id")
        if event_id is None:
            return
        pending = self._pending.get(event_id)
        if pending is not None:
            del self._pending[event_id]

    def _schedule_unclaimed(self, event_id: int) -> None:
        if self._unclaimed is None:
            logger.warning(
                "Reminder %d undelivered but no re-arm handler is registered", event_id
            )
            return
        try:
            asyncio.get_running_loop().create_task(self._unclaimed(event_id))
        except RuntimeError:
            # No running loop (unsubscribe during teardown) — the startup
            # reconcile() re-arms such rows on the next boot.
            logger.info("Reminder %d undelivered at shutdown; reconciled at boot", event_id)


event_hub = EventHub()
