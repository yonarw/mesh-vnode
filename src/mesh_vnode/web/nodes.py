"""The node list, tracks, favourites and requests to one node."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import protocol as proto
from .common import Names, Vnode, require_upstream, rows

router = APIRouter()

# A traceroute is slow by nature: every hop repeats it, and the answer walks back.
EXCHANGE_TIMEOUTS = {"traceroute": 180, "position": 90, "telemetry": 90, "nodeinfo": 90}
# The firmware does not rate-limit requests from the phone API, so this is all
# that stands between a held-down button and a node's airtime.
EXCHANGE_COOLDOWN_S = 30


class FavoriteRequest(BaseModel):
    favorite: bool


class ExchangeRequest(BaseModel):
    kind: Literal["traceroute", "position", "telemetry", "nodeinfo"]
    node: int
    # A node that does not have this channel cannot read the question.
    channel: int = Field(0, ge=0, le=7)


@router.get("/nodes")
def nodes(vnode: Vnode) -> list[dict[str, Any]]:
    my_num = vnode.upstream.my_node_num
    out = rows(vnode.db.nodes())
    for n in out:
        n["has_public_key"] = bool(n.pop("public_key", None))
        n["is_local"] = n["node_num"] == my_num
        n["tracked"] = bool(n["is_local"] or n["is_favorite"])
        n["node_id"] = n.get("node_id") or proto.node_id(n["node_num"])
    return out


@router.get("/nodes/{node_num}/track")
def track(
    vnode: Vnode, node_num: int, hours: int = Query(24 * 7, ge=1, le=24 * 90)
) -> list[dict[str, Any]]:
    """Where a node has been. Kept for this node and the favourites only."""
    return rows(vnode.db.track(node_num, int(time.time()) - hours * 3600))


@router.post("/nodes/{node_num}/favorite")
async def set_favorite(vnode: Vnode, node_num: int, req: FavoriteRequest) -> dict[str, Any]:
    """Star or unstar a node on the physical node, as the app would."""
    require_upstream(vnode)
    try:
        await asyncio.to_thread(vnode.upstream.set_favorite, node_num, req.favorite)
    except Exception as exc:
        raise HTTPException(502, f"could not change favourite: {exc}") from exc
    return {"status": "ok", "node_num": node_num, "favorite": req.favorite}


def exchange_view(found: Iterable[Any], names: Names) -> list[dict[str, Any]]:
    out = rows(found)
    for row in out:
        row["result"] = json.loads(row["result"]) if row["result"] else None
        row["node"] = names.label(row["node_num"])
        if row["kind"] == "traceroute" and row["result"]:
            for key in ("route", "route_back"):
                row["result"][f"{key}_names"] = [
                    names.label(num) for num in row["result"].get(key, [])
                ]
    return out


@router.get("/exchanges")
def exchanges(
    vnode: Vnode, node: int | None = None, limit: int = Query(50, ge=1, le=500)
) -> list[dict[str, Any]]:
    """Traceroutes and position/telemetry/node info requests, newest first.
    Unanswered ones are written off when read rather than on a timer."""
    vnode.db.expire_exchanges(EXCHANGE_TIMEOUTS)
    return exchange_view(vnode.db.exchanges(node, limit=limit), Names(vnode.db))


@router.post("/exchange")
async def exchange(vnode: Vnode, req: ExchangeRequest) -> dict[str, Any]:
    require_upstream(vnode)
    if req.node == vnode.upstream.my_node_num:
        raise HTTPException(400, "that is this node")
    last = vnode.db.last_exchange_ts(req.kind, req.node)
    if last is not None and time.time() - last < EXCHANGE_COOLDOWN_S:
        wait = int(EXCHANGE_COOLDOWN_S - (time.time() - last)) + 1
        raise HTTPException(429, f"just asked - try again in {wait}s")
    try:
        row = await asyncio.to_thread(
            vnode.upstream.send_request, req.kind, req.node, channel_index=req.channel
        )
    except Exception as exc:
        raise HTTPException(502, f"request failed: {exc}") from exc
    return exchange_view([row], Names(vnode.db))[0]
