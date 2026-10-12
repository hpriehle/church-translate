"""WebSocket fan-out for captions (listeners) and live telemetry (operator).

Each client has its own bounded queue and sender task, so one slow phone never delays the
others. Captions are numbered; a phone that reconnects says which number it saw last and
gets what it missed.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("hub")


class _Client:
    def __init__(self, ws: WebSocket, maxsize: int):
        self.ws = ws
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=maxsize)


class Hub:
    def __init__(self, name: str, history: int = 0, queue_size: int = 300):
        self.name = name
        self.clients: set[_Client] = set()
        self.history: deque[dict] = deque(maxlen=history) if history else deque(maxlen=1)
        self.keep_history = history > 0
        self.queue_size = queue_size
        self.dropped_clients = 0

    @property
    def count(self) -> int:
        return len(self.clients)

    def publish(self, msg: dict[str, Any], remember: bool = False) -> None:
        if remember and self.keep_history:
            self.history.append(msg)
        data = json.dumps(msg, ensure_ascii=False)
        for c in list(self.clients):
            try:
                c.queue.put_nowait(data)
            except asyncio.QueueFull:
                self.dropped_clients += 1
                self.clients.discard(c)
                try:
                    c.queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    def replay_after(self, last_seq: int, default_tail: int = 30) -> list[dict]:
        items = list(self.history)
        if last_seq <= 0:
            return items[-default_tail:]
        return [m for m in items if m.get("seq", 0) > last_seq]

    async def serve(self, ws: WebSocket, hello: list[dict], on_message=None) -> None:
        """Run one connection until it closes. `hello` messages are sent first."""
        client = _Client(ws, self.queue_size)
        try:
            for m in hello:
                await ws.send_text(json.dumps(m, ensure_ascii=False))
        except (WebSocketDisconnect, RuntimeError, OSError):
            return  # the phone left while connecting
        self.clients.add(client)

        async def sender():
            while True:
                data = await client.queue.get()
                if data is None:
                    await ws.close(code=1013)
                    return
                await ws.send_text(data)

        async def receiver():
            while True:
                text = await ws.receive_text()
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "ping":
                    client.queue.put_nowait(json.dumps({"type": "pong", "t": msg.get("t")}))
                elif on_message:
                    await on_message(msg)

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError, Exception):
                    pass
            self.clients.discard(client)
