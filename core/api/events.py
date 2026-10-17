"""Indexing progress events for the UI (PLAN.md §10 ``GET /events``).

Phase 0 ships a working event bus and a heartbeat only. That is not a stub: §13.1 makes
reading this stream through WKWebView a Phase 0 acceptance criterion, so the transport,
the framing, and the reconnect behaviour all have to be proven here — before there is
anything interesting to publish. The Phase 3 indexer publishes into the same
:class:`EventBus` without the transport changing.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    """One SSE message."""

    event: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        payload = json.dumps(self.data, separators=(",", ":"))
        return f"event: {self.event}\ndata: {payload}\n\n"


class EventBus:
    """Fan-out to any number of SSE subscribers.

    Each subscriber gets its own bounded queue. The bound matters: the UI may stall while
    rendering, and an unbounded queue would let slow clients grow memory without bound in
    a process that shares a 16 GB budget with the LLM. When a subscriber's queue is full
    the oldest event is dropped, because for progress reporting the newest state is the
    only one worth keeping.
    """

    #: Small on purpose. Progress events are snapshots, not a log.
    MAX_QUEUE = 128

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._history: list[Event] = []
        self._history_limit = 20
        self._lock = asyncio.Lock()

    async def subscribe(self) -> AsyncIterator[Event]:
        """Yield events until the client disconnects."""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self.MAX_QUEUE)
        async with self._lock:
            self._subscribers.add(queue)

        # Replay recent history so a subscriber that connects mid-index immediately knows
        # the current state instead of waiting for the next change.
        for event in list(self._history):
            queue.put_nowait(event)

        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    async def publish(self, event: str, **data: Any) -> None:
        """Broadcast an event to every subscriber."""
        message = Event(event=event, data=data)
        self._history.append(message)
        if len(self._history) > self._history_limit:
            del self._history[: -self._history_limit]

        async with self._lock:
            targets = list(self._subscribers)
        for queue in targets:
            if queue.full():
                # Drop the oldest rather than blocking the publisher.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)

    def publish_sync(self, event: str, **data: Any) -> None:
        """Broadcast from a non-async thread (the Phase 3 index worker).

        ``asyncio.Queue.put_nowait`` is thread-safe, so the worker thread can
        publish progress without a loop. History is trimmed the same way;
        rarest race (two threads trimming at once) only drops an old frame.
        """
        message = Event(event=event, data=data)
        self._history.append(message)
        if len(self._history) > self._history_limit:
            del self._history[: -self._history_limit]
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


#: Process-wide bus. The Phase 3 indexer and the API routes share this instance.
bus = EventBus()


# --------------------------------------------------------------------------------
# SSE framing
# --------------------------------------------------------------------------------


def heartbeat_payload() -> Event:
    """Keep-alive frame.

    Proxies and WKWebView drop idle connections; a comment frame is cheaper than a named
    event because clients can ignore it.
    """
    return Event(event="heartbeat", data={"ts": time.time()})


async def event_stream(bus: EventBus, heartbeat_s: float = 15.0) -> AsyncIterator[str]:
    """Render the bus as an SSE body with heartbeats and client-disconnect handling."""
    stream = bus.subscribe()
    try:
        while True:
            try:
                # `timeout` bounds how long we wait, so a heartbeat still fires when idle.
                event = await asyncio.wait_for(stream.__anext__(), timeout=heartbeat_s)
            except TimeoutError:
                yield heartbeat_payload().to_sse()
                continue
            except StopAsyncIteration:
                return
            yield event.to_sse()
    except asyncio.CancelledError:
        # Normal when the client closes the tab; let the generator finish cleanly so the
        # bus's `finally` unsubscribes.
        raise
    finally:
        await stream.aclose()
