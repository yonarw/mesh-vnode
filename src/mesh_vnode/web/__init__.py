"""HTTP/WebSocket API plus the static web UI.

Read-mostly: the UI shows what the store already knows. The write paths send a
message, ask one node for something, star a node, change settings and clear
the store.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..app import VNodeApp
from ..config import Settings
from . import messages, nodes, system, telemetry
from .messages import SendRequest
from .system import map_choice

__all__ = ["WEBUI_DIST", "SendRequest", "create_app", "map_choice"]

WEBUI_DIST = Path(__file__).resolve().parents[3] / "webui" / "dist"


class Hub:
    """Fan-out of live events to connected browsers. Call on the event loop."""

    def __init__(self) -> None:
        self.sockets: set[WebSocket] = set()
        self._sends: set[asyncio.Task] = set()

    def publish(self, event: str, payload: dict[str, Any]) -> None:
        if not self.sockets:
            return
        msg = json.dumps({"event": event, "payload": payload, "ts": time.time()})
        for ws in list(self.sockets):
            task = asyncio.create_task(self._send(ws, msg))
            self._sends.add(task)
            task.add_done_callback(self._sends.discard)

    async def _send(self, ws: WebSocket, msg: str) -> None:
        try:
            await ws.send_text(msg)
        except Exception:
            self.sockets.discard(ws)


def create_app(settings: Settings, *, cli_upstream: bool = False) -> FastAPI:
    vnode = VNodeApp(settings, cli_upstream=cli_upstream)
    hub = Hub()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        loop = asyncio.get_running_loop()
        vnode.server.add_listener(hub.publish)
        vnode.upstream.add_event_sink(
            lambda event, payload: loop.call_soon_threadsafe(hub.publish, event, payload)
        )
        vnode.upstream.add_state_sink(
            lambda state, detail: loop.call_soon_threadsafe(
                hub.publish, "upstream", {"state": state, "detail": detail}
            )
        )
        await vnode.start()
        try:
            yield
        finally:
            await vnode.stop()

    api = FastAPI(title="mesh-vnode", version=__version__, lifespan=lifespan)
    api.state.vnode = vnode
    for module in (system, messages, nodes, telemetry):
        api.include_router(module.router, prefix="/api")

    @api.websocket("/api/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        hub.sockets.add(socket)
        try:
            hello = {"event": "hello", "payload": system.status(vnode)}
            await socket.send_text(json.dumps(hello))
            with contextlib.suppress(WebSocketDisconnect):
                while True:
                    await socket.receive_text()
        finally:
            hub.sockets.discard(socket)

    if WEBUI_DIST.is_dir():
        dist = WEBUI_DIST.resolve()
        api.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @api.get("/{full_path:path}")
        def spa(full_path: str):
            candidate = (dist / full_path).resolve()
            if full_path and candidate.is_relative_to(dist) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")
    else:

        @api.get("/")
        def no_ui() -> dict[str, str]:
            return {
                "status": "api only",
                "hint": "run `npm --prefix webui install && npm --prefix webui run build`",
            }

    return api
