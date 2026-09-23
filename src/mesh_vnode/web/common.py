"""What every route module needs: the app object and node names."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Any

from fastapi import Depends, HTTPException
from fastapi.requests import HTTPConnection

from .. import protocol as proto
from ..app import VNodeApp
from ..db import Database


def get_vnode(conn: HTTPConnection) -> VNodeApp:
    return conn.app.state.vnode


Vnode = Annotated[VNodeApp, Depends(get_vnode)]


def rows(items: Iterable[Any]) -> list[dict[str, Any]]:
    """Database rows as dicts, without the raw frame bytes."""
    out = []
    for row in items:
        d = dict(row)
        d.pop("raw", None)
        out.append(d)
    return out


def require_upstream(vnode: VNodeApp) -> None:
    if not vnode.upstream.connected.is_set():
        raise HTTPException(503, "upstream node not connected")


class Names:
    """Every known node, for labelling node numbers in a response."""

    def __init__(self, db: Database) -> None:
        self.nodes = {n["node_num"]: n for n in db.nodes()}

    def label(self, num: int) -> dict[str, Any]:
        node = self.nodes.get(num)
        return {
            "short": (node["short_name"] if node else None) or None,
            "long": (node["long_name"] if node else None) or None,
            "id": proto.node_id(num),
        }
