"""Telemetry series and the mesh traffic chart."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Query

from .. import series
from .common import Vnode, rows

router = APIRouter()


@router.get("/telemetry/nodes")
def telemetry_nodes(vnode: Vnode) -> list[dict[str, Any]]:
    """This node first, then the favourites: telemetry for anyone else is not stored."""
    my_num = vnode.upstream.my_node_num
    out = rows(vnode.db.telemetry_nodes())
    for r in out:
        r["is_local"] = r["node_num"] == my_num
    out = [r for r in out if r["is_local"] or r["is_favorite"]]
    out.sort(key=lambda r: (not r["is_local"], -(r["last_time"] or 0)))
    return out


@router.get("/telemetry")
def telemetry(
    vnode: Vnode,
    node: int | None = None,
    hours: int = Query(24, ge=1, le=24 * 90),
    kind: str | None = None,
    points: int = Query(360, ge=10, le=5000),
) -> list[dict[str, Any]]:
    """Samples for one node, with counter rates derived and each kind averaged
    down to about `points` samples."""
    since = int(time.time()) - hours * 3600
    found = series.add_rates(vnode.db.telemetry_rows(node, since, limit=500_000))
    if kind:
        found = [r for r in found if r["kind"] == kind]
    found = series.bucket(found, hours * 3600, points)
    return [{k: v for k, v in r.items() if v is not None} for r in found]


@router.get("/traffic")
def traffic(vnode: Vnode, hours: int = Query(24, ge=1, le=24 * 30)) -> list[dict[str, Any]]:
    """Packets heard per hour and portnum, across the whole mesh."""
    return rows(vnode.db.traffic(int(time.time()) - hours * 3600))
