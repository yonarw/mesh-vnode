"""Service status, connected apps, the event log, settings and clearing the store."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from .. import __version__
from ..app import VNodeApp
from ..upstream import is_own_virtual_node
from .common import Vnode, rows

logger = logging.getLogger(__name__)

router = APIRouter()

MapProvider = Literal["openfreemap", "carto"]
# "positron" exists on both providers, so a style alone does not identify one.
MAP_STYLES: dict[str, tuple[str, ...]] = {
    "openfreemap": ("dark", "liberty", "bright", "positron", "fiord"),
    "carto": ("dark-matter", "positron", "voyager"),
}
DisplayMode = Literal["graph", "number", "hidden"]


def map_choice(stored: dict[str, Any]) -> tuple[str, str]:
    """The map provider and style to use, tolerant of what is stored.

    A database from before the provider setting holds only a style and perhaps
    a key; if either is CARTO's it stays on CARTO. A style the provider does
    not have falls back to that provider's first one.
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


class PrefsPatch(BaseModel):
    """Web-UI settings. A field left out is unchanged; null resets it to the
    default (for the node address: back to VNODE_UPSTREAM_HOST)."""

    upstream_host: str | None = Field(default=None, min_length=1, max_length=253)
    upstream_port: int | None = Field(default=None, ge=1, le=65535)
    carto_api_key: str | None = Field(default=None, max_length=200)
    map_provider: MapProvider | None = None
    map_style: str | None = None
    # Conversation keys: "ch:<index>" or "dm:<node_num>".
    muted: list[str] | None = None
    # Telemetry widget id -> how it is shown.
    telemetry_display: dict[str, DisplayMode] | None = None
    allow_admin: bool | None = None
    app_forward: AppForward | None = None

    @field_validator("map_style")
    @classmethod
    def _known_style(cls, style: str | None) -> str | None:
        if style is not None and not any(style in styles for styles in MAP_STYLES.values()):
            raise ValueError(f"unknown map style: {style}")
        return style


class ClearRequest(BaseModel):
    confirm: Literal["CLEAR"]


@router.get("/status")
def status(vnode: Vnode) -> dict[str, Any]:
    my_num = vnode.upstream.my_node_num
    me = vnode.db.node(my_num) if my_num else None
    return {
        "version": __version__,
        "upstream": {
            **vnode.upstream.status(),
            "my_short_name": me["short_name"] if me else None,
            "my_long_name": me["long_name"] if me else None,
        },
        "vnode": vnode.server.status(),
        "counts": vnode.db.counts(),
        "settings": {
            "replay_mode": vnode.settings.replay_mode,
            "replay_limit": vnode.settings.replay_limit,
            "client_key_mode": vnode.settings.client_key_mode,
            "allow_admin": vnode.server.allow_admin,
            "retention_days": vnode.settings.retention_days,
        },
    }


@router.get("/clients")
def clients(vnode: Vnode) -> dict[str, Any]:
    return {
        "connected": vnode.server.status()["clients"],
        "known": rows(vnode.db.clients()),
        "max_seq": vnode.db.max_seq(),
    }


@router.post("/clients/{client_key}/reset")
def reset_client(vnode: Vnode, client_key: str) -> dict[str, str]:
    """Rewind a client's cursor so the next connect replays everything."""
    vnode.db.reset_client_cursor(client_key)
    return {"status": "ok", "client_key": client_key}


@router.get("/events")
def events(vnode: Vnode, limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
    return rows(vnode.db.events(limit))


def prefs_view(vnode: VNodeApp) -> dict[str, Any]:
    stored = vnode.db.prefs()
    map_provider, map_style = map_choice(stored)
    if vnode.cli_upstream:
        source = "command line"
    elif "upstream_host" in stored:
        source = "web ui"
    else:
        source = "environment"
    fallback_host, fallback_port = vnode.upstream_fallback
    return {
        "upstream_host": vnode.upstream.host,
        "upstream_port": vnode.upstream.port,
        "upstream_source": source,
        "upstream_fallback": f"{fallback_host}:{fallback_port}",
        "upstream_fallback_set": vnode.upstream_fallback_set,
        "carto_api_key": stored.get("carto_api_key", ""),
        "map_provider": map_provider,
        "map_style": map_style,
        "muted": stored.get("muted", []),
        "telemetry_display": stored.get("telemetry_display", {}),
        "allow_admin": vnode.server.allow_admin,
        "app_forward": vnode.server.forward_policy,
    }


@router.get("/prefs")
def get_prefs(vnode: Vnode) -> dict[str, Any]:
    return prefs_view(vnode)


@router.put("/prefs")
def put_prefs(vnode: Vnode, patch: PrefsPatch) -> dict[str, Any]:
    given = patch.model_dump(exclude_unset=True)
    moves_upstream = bool({"upstream_host", "upstream_port"} & given.keys())
    if moves_upstream and vnode.cli_upstream:
        raise HTTPException(409, "the node address was given on the command line (--node) and wins")
    if moves_upstream:
        # A field left out keeps its current value, null falls back to the environment.
        fallback_host, fallback_port = vnode.upstream_fallback
        host = given.get("upstream_host", vnode.upstream.host) or fallback_host
        port = given.get("upstream_port", vnode.upstream.port) or fallback_port
        if is_own_virtual_node(host.strip(), port, vnode.settings.listen_port):
            raise HTTPException(
                422,
                f"{host}:{port} is this service's own virtual node - enter the real "
                f"node's address (its port is usually 4403)",
            )
    for key, value in given.items():
        vnode.db.set_pref(key, value.strip() if isinstance(value, str) else value)
    if "allow_admin" in given:
        vnode.apply_admin_pref()
    if "app_forward" in given:
        vnode.apply_forward_pref()
    if moves_upstream:
        vnode.upstream.retarget(*vnode.upstream_target())
    return prefs_view(vnode)


@router.get("/clear")
def clear_preview(vnode: Vnode) -> dict[str, Any]:
    """What POST /api/clear would delete, for the confirmation dialog."""
    return {**vnode.db.clear_preview(), "connected apps": len(vnode.server.clients)}


@router.post("/clear")
async def clear(vnode: Vnode, req: ClearRequest) -> dict[str, Any]:
    """Delete everything stored, drop the connected apps and re-read the node.
    Settings are kept."""
    removed = await asyncio.to_thread(vnode.db.clear_data)
    # Before a live message could move a cursor that no longer exists.
    removed["connected apps"] = vnode.server.disconnect_all()
    logger.warning("vnode: all stored data cleared from the web UI: %s", removed)
    vnode.db.log_event("warn", "web", "all stored data cleared; re-reading the node")
    vnode.upstream.recapture()
    return {"status": "ok", "removed": removed}
