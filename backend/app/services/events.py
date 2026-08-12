"""In-process SSE fan-out.

One queue per connected client rather than a single shared queue: with a shared
queue the first reader to wake consumes the event and every other client misses
it. Queues are bounded so a client that stops reading (a backgrounded phone,
a paused tab) cannot grow memory without limit — it drops the oldest event
instead, which for reminders is the right trade.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# Enough slack for a short background pause without buffering an unbounded
# backlog for a client that is never coming back.
MAX_QUEUED_EVENTS = 32


class EventHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUED_EVENTS)
        self._subscribers.add(queue)
        logger.info("SSE client connected (%d total)", len(self._subscribers))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)
        logger.info("SSE client disconnected (%d remaining)", len(self._subscribers))

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: dict) -> None:
        """Fan an event out to every connected client. Never blocks."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest so a stalled client still gets recent events.
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    logger.warning("Dropped event for a stalled SSE client")


event_hub = EventHub()
