"""Console fan-out.

``ConsoleHub`` keeps, per server, a bounded ring buffer of recent output lines
and a set of live subscriber queues. ``ServerProcess`` publishes each stdout
line; WebSocket connections replay the history then drain their queue. Kept
transport-agnostic (plain ``asyncio.Queue``) so the process layer never imports
FastAPI.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict, deque

_HISTORY = 500  # lines of scrollback retained per server
_QUEUE_MAX = 2000  # per-subscriber backlog before lines are dropped


class ConsoleHub:
    def __init__(self, history: int = _HISTORY) -> None:
        self._maxlen = history
        self._history: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=history))
        self._subs: dict[str, set[asyncio.Queue[str]]] = defaultdict(set)

    def publish(self, server_id: str, line: str) -> None:
        self._history[server_id].append(line)
        for queue in list(self._subs.get(server_id, ())):
            # Drop lines for a subscriber that has fallen too far behind rather
            # than blocking the process's stdout pump.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(line)

    def history(self, server_id: str) -> list[str]:
        return list(self._history.get(server_id, ()))

    def subscribe(self, server_id: str) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._subs[server_id].add(queue)
        return queue

    def unsubscribe(self, server_id: str, queue: asyncio.Queue[str]) -> None:
        self._subs[server_id].discard(queue)

    def clear(self, server_id: str) -> None:
        """Drop scrollback (used when a server is (re)started)."""
        self._history.pop(server_id, None)
