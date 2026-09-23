"""HTTP/WebSocket API plus the static web UI.

Read-mostly: the UI shows what the store already knows. The write paths are
`POST /api/send`, which is there so the whole upstream send path can be exercised
without a phone, and `POST /api/exchange`, which asks one node for a traceroute,
its position, its telemetry or its node info.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, series
from . import protocol as proto
from .app import VNodeApp
from .config import Settings
from .upstream import is_own_virtual_node

logger = logging.getLogger(__name__)

WEBUI_DIST = Path(__file__).resolve().parent.parent.parent / "webui" / "dist"

# How long a request may stay open before it is written off. A traceroute is
# slow by nature: every hop repeats it, and the answer walks the same way back.
EXCHANGE_TIMEOUTS = {"traceroute": 180, "position": 90, "telemetry": 90, "nodeinfo": 90}
# One request per node and kind per half minute. The firmware does not limit
# what comes in over the phone API - its own traceroute cooldown guards the
# device's screen menu only - so this is the only thing standing between a
# held-down button and a node's airtime.
EXCHANGE_COOLDOWN_S = 30


def _rows(rows) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        d = dict(row)
        d.pop("raw", None)
        out.append(d)
    return out


class SendRequest(BaseModel):
    text: str = Field(min_length=1, max_length=228)
    channel: int = 0
    to: int | str = proto.BROADCAST_NUM
    want_ack: bool = True
    # A reaction: `text` is the emoji itself and `reply_id` is the packet id of
    # the message it belongs to. The pair travels as an ordinary text packet -
    # see Upstream.send_text.
    reply_id: int | None = None
    emoji: bool = False


class ExchangeRequest(BaseModel):
    kind: Literal["traceroute", "position", "telemetry", "nodeinfo"]
    node: int
    # Which channel's key encrypts the request. The phone apps always use the
    # primary; a node that does not have the channel picked here cannot read
    # the question and will not answer.
    channel: int = Field(0, ge=0, le=7)


class FavoriteRequest(BaseModel):
    favorite: bool


class ClearRequest(BaseModel):
    # Typed by hand in the web UI; anything else is refused.
    confirm: Literal["CLEAR"]


MapProvider = Literal["openfreemap", "carto"]
# Basemap styles per provider. OpenFreeMap serves OpenStreetMap-derived vector
# tiles with no key and no account; CARTO looks the same but wants a key for
# anything but casual use. "positron" exists on both, so a style alone does not
# identify the provider.
MAP_STYLES: dict[str, tuple[str, ...]] = {
    "openfreemap": ("dark", "liberty", "bright", "positron", "fiord"),
    "carto": ("dark-matter", "positron", "voyager"),
}
MapStyle = Literal["dark", "liberty", "bright", "positron", "fiord", "dark-matter", "voyager"]


def map_choice(stored: dict[str, Any]) -> tuple[str, str]:
    """The map provider and style to use, tolerant of what is stored.

    A database written before the provider setting existed holds only a style
    (and perhaps a key); if either is CARTO's it stays on CARTO, so an upgrade
    does not silently change the map under the user. A style that does not
    belong to the provider falls back to that provider's first one.
    """
    provider = stored.get("map_provider")
    if provider not in MAP_STYLES:
        was_carto = stored.get("map_style") in ("dark-matter", "voyager") or bool(
            stored.get("carto_api_key")
        )
        provider = "carto" if was_carto else "openfreemap"
    styles = MAP_STYLES[provider]
    style = stored.get("map_style")
    return provider, style if style in styles else styles[0]


class AppForward(BaseModel):
    """Live traffic sent to connected apps, per category."""

    position: Literal["all", "favorites", "none"] = "all"
    telemetry: Literal["all", "favorites", "none"] = "all"
    nodeinfo: Literal["all", "none"] = "all"
    other: Literal["all", "none"] = "all"


DisplayMode = Literal["graph", "number", "hidden"]


class PrefsPatch(BaseModel):
    """Web-UI settings. A field left out is unchanged; null resets it to the
    default (for the node address: back to VNODE_UPSTREAM_HOST)."""

    upstream_host: str | None = Field(default=None, min_length=1, max_length=253)
    upstream_port: int | None = Field(default=None, ge=1, le=65535)
    carto_api_key: str | None = Field(default=None, max_length=200)
    map_provider: MapProvider | None = None
    map_style: MapStyle | None = None
    # Conversation keys: "ch:<index>" or "dm:<node_num>".
    muted: list[str] | None = None
    # Telemetry widget id -> how it is shown.
    telemetry_display: dict[str, DisplayMode] | None = None
    # Let apps change the node's settings through the virtual node.
    allow_admin: bool | None = None
    app_forward: AppForward | None = None


class Hub:
    """Fan-out of live events to connected browsers."""

    def __init__(self) -> None:
        self.sockets: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def publish(self, event: str, payload: dict[str, Any]) -> None:
        if not self.sockets:
            return
        msg = json.dumps({"event": event, "payload": payload, "ts": time.time()})
        for ws in list(self.sockets):
            asyncio.create_task(self._send(ws, msg))

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
        hub.loop = asyncio.get_running_loop()
        vnode.server.add_listener(hub.publish)
        vnode.upstream.add_event_sink(
            lambda event, payload: hub.loop.call_soon_threadsafe(hub.publish, event, payload)
        )
        vnode.upstream.add_state_sink(
            lambda state, detail: hub.loop.call_soon_threadsafe(
                hub.publish, "upstream", {"state": state, "detail": detail}
            )
        )
        await vnode.start()
        try:
            yield
        finally:
            await vnode.stop()

    api = FastAPI(title="mesh-vnode", version=__version__, lifespan=lifespan)
    db = vnode.db

    def _my_names() -> dict[str, Any]:
        my_num = vnode.upstream.my_node_num
        node = db.node(my_num) if my_num else None
        return {
            "my_short_name": node["short_name"] if node else None,
            "my_long_name": node["long_name"] if node else None,
        }

    def _lora():
        rows = db.config_frames(kinds=["config"])
        for row in rows:
            fr = proto.parse_from_radio(row["raw"])
            if fr.config.WhichOneof("payload_variant") == "lora":
                return fr.config.lora
        return None

    # ------------------------------------------------------------- read API

    @api.get("/api/status")
    def status() -> dict[str, Any]:
        return {
            "version": __version__,
            "upstream": {**vnode.upstream.status(), **_my_names()},
            "vnode": vnode.server.status(),
            "counts": db.counts(),
            "settings": {
                "replay_mode": settings.replay_mode,
                "replay_limit": settings.replay_limit,
                "client_key_mode": settings.client_key_mode,
                "allow_admin": settings.allow_admin,
                "retention_days": settings.retention_days,
            },
        }

    @api.get("/api/channels")
    def channels() -> list[dict[str, Any]]:
        frames = [proto.parse_from_radio(r["raw"]).channel for r in db.config_frames(["channel"])]
        return proto.channel_info(frames, _lora())

    @api.get("/api/nodes")
    def nodes() -> list[dict[str, Any]]:
        my_num = vnode.upstream.my_node_num
        out = _rows(db.nodes())
        for n in out:
            # Raw key bytes are not JSON; whether there is one is what matters.
            n["has_public_key"] = bool(n.pop("public_key", None))
            n["is_local"] = n["node_num"] == my_num
            # Only these keep a position history (see `positions`).
            n["tracked"] = bool(n["is_local"] or n["is_favorite"])
            n["node_id"] = n.get("node_id") or proto.node_id(n["node_num"])
        return out

    @api.get("/api/nodes/{node_num}/track")
    def track(node_num: int, hours: int = Query(24 * 7, ge=1, le=24 * 90)) -> list[dict[str, Any]]:
        """Where a node has been. Kept for this node and the favourites only."""
        since = int(time.time()) - hours * 3600
        return [dict(r) for r in db.track(node_num, since)]

    @api.post("/api/nodes/{node_num}/favorite")
    async def set_favorite(node_num: int, req: FavoriteRequest) -> dict[str, Any]:
        """Star or unstar a node on the physical node, as the app would."""
        if not vnode.upstream.connected.is_set():
            raise HTTPException(503, "upstream node not connected")
        try:
            await asyncio.to_thread(vnode.upstream.set_favorite, node_num, req.favorite)
        except Exception as exc:
            raise HTTPException(502, f"could not change favourite: {exc}") from exc
        return {"status": "ok", "node_num": node_num, "favorite": req.favorite}

    def _names() -> dict[int, sqlite3.Row]:
        return {n["node_num"]: n for n in db.nodes()}

    def _label(names: dict[int, Any], num: int) -> dict[str, Any]:
        node = names.get(num)
        return {
            "short": (node["short_name"] if node else None) or None,
            "long": (node["long_name"] if node else None) or None,
            "id": proto.node_id(num),
        }

    @api.get("/api/messages")
    def messages(
        limit: int = Query(200, ge=1, le=1000),
        before_seq: int | None = None,
        channel: int | None = None,
        node: int | None = None,
    ) -> list[dict[str, Any]]:
        """Messages oldest first, with sender names and reactions folded in.

        A reaction is its own text packet (emoji set, reply_id pointing at the
        target). It is attached to the message it reacts to rather than listed
        on its own; one whose target is not in this page stays in the list,
        flagged, so it is not silently lost.
        """
        rows = _rows(
            db.messages(limit=limit, before_seq=before_seq, channel=channel, node_num=node)
        )
        names = _names()
        for r in rows:
            r["from_id"] = proto.node_id(r["from_num"])
            r["to_id"] = (
                "^all" if r["to_num"] == proto.BROADCAST_NUM else proto.node_id(r["to_num"])
            )
            r["is_dm"] = r["to_num"] != proto.BROADCAST_NUM
            r["sender"] = _label(names, r["from_num"])
            r["reactions"] = []

        by_packet = {r["packet_id"]: r for r in rows if not r["emoji"]}
        out = []
        for r in reversed(rows):
            target = by_packet.get(r["reply_id"]) if r["emoji"] and r["reply_id"] else None
            if target is not None:
                target["reactions"].append(
                    {"emoji": r["text"], "from_num": r["from_num"], "sender": r["sender"]}
                )
                continue
            r["is_reaction"] = bool(r["emoji"])
            out.append(r)
        return out

    @api.get("/api/messages/{seq}")
    def message_details(seq: int) -> dict[str, Any]:
        """One message with everything known about it: reception details for
        a message from the mesh, the delivery log for one we sent."""
        row = db.packet(seq)
        if row is None:
            raise HTTPException(404, "no such message")
        msg = _rows([row])[0]
        names = _names()
        msg["sender"] = _label(names, msg["from_num"])
        msg["recipient"] = (
            None if msg["to_num"] == proto.BROADCAST_NUM else _label(names, msg["to_num"])
        )
        msg["is_dm"] = msg["to_num"] != proto.BROADCAST_NUM
        log = []
        for entry in _rows(db.delivery_log(msg["packet_id"], msg["from_num"])):
            if entry["ack_from"] is not None:
                entry["ack_from_name"] = _label(names, entry["ack_from"])
            if entry["relay_node"]:
                # Only the last byte of the relayer's number travels in the
                # packet. Direct neighbours first: they are the likely ones.
                matches = [n for n in names.values() if n["node_num"] & 0xFF == entry["relay_node"]]
                matches.sort(key=lambda n: (n["hops_away"] != 0, -(n["last_heard"] or 0)))
                entry["relay_candidates"] = [_label(names, n["node_num"]) for n in matches[:5]]
            log.append(entry)
        msg["delivery"] = log
        return msg

    @api.get("/api/conversations")
    def conversations() -> dict[str, Any]:
        my_num = vnode.upstream.my_node_num or 0
        peers: dict[int, dict[str, Any]] = {}
        for row in db.messages(limit=2000):
            if row["emoji"]:
                continue
            if row["to_num"] == proto.BROADCAST_NUM:
                continue
            peer = row["from_num"] if row["from_num"] != my_num else row["to_num"]
            entry = peers.setdefault(
                peer, {"node_num": peer, "count": 0, "last_time": 0, "last_text": None}
            )
            entry["count"] += 1
            if row["rx_time"] > entry["last_time"]:
                entry["last_time"] = row["rx_time"]
                entry["last_text"] = row["text"]
        names = {n["node_num"]: n for n in db.nodes()}
        for num, entry in peers.items():
            node = names.get(num)
            entry["long_name"] = node["long_name"] if node else None
            entry["short_name"] = node["short_name"] if node else None
            entry["node_id"] = proto.node_id(num)
        return {
            "channels": channels(),
            "direct": sorted(peers.values(), key=lambda e: e["last_time"], reverse=True),
        }

    @api.get("/api/telemetry/nodes")
    def telemetry_nodes() -> list[dict[str, Any]]:
        """The nodes the telemetry page offers: this one first, then favourites.
        Telemetry for anything else is not stored at all."""
        my_num = vnode.upstream.my_node_num
        rows = _rows(db.telemetry_nodes())
        for r in rows:
            r["is_local"] = r["node_num"] == my_num
        rows = [r for r in rows if r["is_local"] or r["is_favorite"]]
        rows.sort(key=lambda r: (not r["is_local"], -(r["last_time"] or 0)))
        return rows

    @api.get("/api/telemetry")
    def telemetry(
        node: int | None = None,
        hours: int = Query(24, ge=1, le=24 * 90),
        kind: str | None = None,
        points: int = Query(360, ge=10, le=5000),
    ) -> list[dict[str, Any]]:
        """Samples for one node, with counter rates derived and the series
        averaged down to about `points` per kind."""
        since = int(time.time()) - hours * 3600
        rows = series.add_rates(db.telemetry_rows(node, since, limit=500_000))
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        rows = series.bucket(rows, hours * 3600, points)
        # Each kind fills only its own columns; the rest are empty weight.
        return [{k: v for k, v in r.items() if v is not None} for r in rows]

    @api.get("/api/traffic")
    def traffic(hours: int = Query(24, ge=1, le=24 * 30)) -> list[dict[str, Any]]:
        """Packets heard per hour and portnum, across the whole mesh."""
        since = int(time.time()) - hours * 3600
        return [dict(r) for r in db.traffic(since)]

    @api.get("/api/clients")
    def clients() -> dict[str, Any]:
        return {
            "connected": vnode.server.status()["clients"],
            "known": _rows(db.clients()),
            "max_seq": db.max_seq(),
        }

    @api.post("/api/clients/{client_key}/reset")
    def reset_client(client_key: str) -> dict[str, str]:
        """Rewind a client's cursor so the next connect replays everything.

        A debugging aid: it shows what a client does with a backlog it has
        already seen.
        """
        db.reset_client_cursor(client_key)
        return {"status": "ok", "client_key": client_key}

    @api.get("/api/events")
    def events(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return _rows(db.events(limit))

    # -------------------------------------------------------------- exchanges

    def _exchange_view(rows: list[sqlite3.Row], names: dict[int, Any]) -> list[dict[str, Any]]:
        out = []
        for row in _rows(rows):
            row["result"] = json.loads(row["result"]) if row["result"] else None
            row["node"] = _label(names, row["node_num"])
            if row["kind"] == "traceroute" and row["result"]:
                for key in ("route", "route_back"):
                    row["result"][f"{key}_names"] = [
                        _label(names, num) for num in row["result"].get(key, [])
                    ]
            out.append(row)
        return out

    @api.get("/api/exchanges")
    def exchanges(
        node: int | None = None,
        limit: int = Query(50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        """Traceroutes and position/telemetry/node info requests, newest first.

        Requests nothing answered in time are written off here rather than on a
        timer: a row only has to look finished by the time somebody reads it.
        """
        db.expire_exchanges(EXCHANGE_TIMEOUTS)
        return _exchange_view(db.exchanges(node, limit=limit), _names())

    @api.post("/api/exchange")
    async def exchange(req: ExchangeRequest) -> dict[str, Any]:
        if not vnode.upstream.connected.is_set():
            raise HTTPException(503, "upstream node not connected")
        my_num = vnode.upstream.my_node_num
        if req.node == my_num:
            raise HTTPException(400, "that is this node")
        last = db.last_exchange_ts(req.kind, req.node)
        if last is not None and time.time() - last < EXCHANGE_COOLDOWN_S:
            wait = int(EXCHANGE_COOLDOWN_S - (time.time() - last)) + 1
            raise HTTPException(429, f"just asked - try again in {wait}s")
        try:
            row = await asyncio.to_thread(
                vnode.upstream.send_request,
                req.kind,
                req.node,
                channel_index=req.channel,
            )
        except Exception as exc:
            raise HTTPException(502, f"request failed: {exc}") from exc
        return _exchange_view([row], _names())[0]

    # ------------------------------------------------------------------ prefs

    def _prefs_view() -> dict[str, Any]:
        stored = db.prefs()
        map_provider, map_style = map_choice(stored)
        host_source = (
            "command line"
            if vnode.cli_upstream
            else "web ui"
            if "upstream_host" in stored
            else "environment"
        )
        env_host, env_port = vnode._env_upstream
        return {
            "upstream_host": settings.upstream_host,
            "upstream_port": settings.upstream_port,
            "upstream_source": host_source,
            # Where "use the environment setting again" would point, and
            # whether anything set it: with no VNODE_UPSTREAM_HOST that button
            # falls back to a built-in default that resolves nowhere.
            "upstream_fallback": f"{env_host}:{env_port}",
            "upstream_fallback_set": vnode._env_upstream_set,
            "carto_api_key": stored.get("carto_api_key", ""),
            "map_provider": map_provider,
            "map_style": map_style,
            "muted": stored.get("muted", []),
            "telemetry_display": stored.get("telemetry_display", {}),
            "allow_admin": settings.allow_admin,
            "app_forward": vnode.server.forward_policy,
        }

    @api.get("/api/prefs")
    def get_prefs() -> dict[str, Any]:
        return _prefs_view()

    @api.put("/api/prefs")
    def put_prefs(patch: PrefsPatch) -> dict[str, Any]:
        given = patch.model_dump(exclude_unset=True)
        moves_upstream = bool({"upstream_host", "upstream_port"} & given.keys())
        if moves_upstream and vnode.cli_upstream:
            raise HTTPException(
                409, "the node address was given on the command line (--node) and wins"
            )
        if moves_upstream:
            # Where the link would point after this change: a field left out
            # keeps its current value, null falls back to the environment.
            env_host, env_port = vnode._env_upstream
            host = given.get("upstream_host", settings.upstream_host) or env_host
            port = given.get("upstream_port", settings.upstream_port) or env_port
            if is_own_virtual_node(host.strip(), port, settings.listen_port):
                raise HTTPException(
                    422,
                    f"{host}:{port} is this service's own virtual node - enter the real "
                    f"node's address (its port is usually 4403)",
                )
        for key, value in given.items():
            db.set_pref(key, value.strip() if isinstance(value, str) else value)
        if "allow_admin" in given:
            vnode.apply_admin_pref()
        if "app_forward" in given:
            vnode.apply_forward_pref()
        if moves_upstream:
            host, port = vnode.upstream_target()
            vnode.upstream.retarget(host, port)
        return _prefs_view()

    @api.get("/api/clear")
    def clear_preview() -> dict[str, Any]:
        """What POST /api/clear would delete, for the confirmation dialog."""
        return {
            **db.clear_preview(),
            "connected apps": len(vnode.server.status()["clients"]),
        }

    @api.post("/api/clear")
    async def clear(req: ClearRequest) -> dict[str, Any]:
        """Start over: delete everything stored, drop the connected apps (they
        reconnect by themselves) and fetch the node's config and node list
        afresh. Settings are kept."""
        removed = await asyncio.to_thread(db.clear_data)
        # Straight after the clear, before a live message could move a cursor
        # that no longer exists.
        removed["connected apps"] = vnode.server.disconnect_all()
        logger.warning("vnode: all stored data cleared from the web UI: %s", removed)
        db.log_event("warn", "web", "all stored data cleared; re-reading the node")
        vnode.upstream.recapture()
        return {"status": "ok", "removed": removed}

    # ------------------------------------------------------------- write API

    @api.post("/api/send")
    async def send(req: SendRequest) -> dict[str, Any]:
        if not vnode.upstream.connected.is_set():
            raise HTTPException(503, "upstream node not connected")
        try:
            packet = await asyncio.to_thread(
                vnode.upstream.send_text,
                req.text,
                destination=req.to,
                channel_index=req.channel,
                want_ack=req.want_ack,
                reply_id=req.reply_id,
                emoji=req.emoji,
            )
        except Exception as exc:
            raise HTTPException(502, f"send failed: {exc}") from exc
        seq = await asyncio.to_thread(vnode.upstream.store_client_packet, packet, "webui")
        if seq is not None:
            vnode.server.mirror_outgoing(packet, seq)
        return {"status": "ok", "packet_id": packet.id, "seq": seq}

    # ------------------------------------------------------------- websocket

    @api.websocket("/api/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        hub.sockets.add(socket)
        try:
            await socket.send_text(json.dumps({"event": "hello", "payload": status()}))
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            hub.sockets.discard(socket)

    # ----------------------------------------------------------------- static

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

    api.state.vnode = vnode
    return api
