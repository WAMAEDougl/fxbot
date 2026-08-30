"""Async event bus (design section 3: "everything in the execution plane is an
event on a bus").

Design constraints:

* Same bus in backtest and live. The backtester feeds events in venue-time order
  and drives the loop; live feeds push events as they arrive.
* Deterministic dispatch: handlers for a given event type fire in subscription
  order, and ``publish`` fully drains all downstream events it triggers before
  returning. This is what makes "replay the same tape twice -> identical orders"
  hold (a property test in tests/property/).
* A handler raising does not kill the bus. The exception is captured, routed to
  the error hook (used to disable a crashing strategy per design 4.3), and
  dispatch continues.
"""

from __future__ import annotations

import inspect
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from core.events import Event

Handler = Callable[[Event], Awaitable[None] | None]
ErrorHook = Callable[[type, Handler, Event, BaseException], Awaitable[None] | None]


class EventBus:
    def __init__(self, *, error_hook: ErrorHook | None = None) -> None:
        self._subs: dict[type, list[Handler]] = defaultdict(list)
        self._wildcard: list[Handler] = []  # receive every event (audit log)
        self._queue: deque[Event] = deque()
        self._dispatching = False
        self._error_hook = error_hook
        self.published_count = 0

    # -- wiring ----------------------------------------------------------- #
    def subscribe(self, event_type: type, handler: Handler) -> None:
        self._subs[event_type].append(handler)

    def subscribe_all(self, handler: Handler) -> None:
        self._wildcard.append(handler)

    def unsubscribe(self, event_type: type, handler: Handler) -> None:
        if handler in self._subs.get(event_type, []):
            self._subs[event_type].remove(handler)

    # -- publish -------------------------------------------------------- #
    async def publish(self, event: Event) -> None:
        """Enqueue ``event`` and drain the queue (including cascades)."""
        self._queue.append(event)
        if self._dispatching:
            return
        self._dispatching = True
        try:
            while self._queue:
                current = self._queue.popleft()
                await self._dispatch(current)
        finally:
            self._dispatching = False

    async def _dispatch(self, event: Event) -> None:
        self.published_count += 1
        handlers = self._wildcard + self._subs.get(type(event), [])
        for handler in handlers:
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:  # noqa: BLE001 - deliberate: bus must survive
                await self._on_error(type(event), handler, event, exc)

    async def _on_error(
        self, etype: type, handler: Handler, event: Event, exc: BaseException
    ) -> None:
        if self._error_hook is None:
            raise exc
        result = self._error_hook(etype, handler, event, exc)
        if inspect.isawaitable(result):
            await result

    # -- introspection ------------------------------------------------- #
    def stats(self) -> dict[str, Any]:
        return {
            "published_count": self.published_count,
            "subscriptions": {t.__name__: len(h) for t, h in self._subs.items()},
            "wildcard": len(self._wildcard),
        }
