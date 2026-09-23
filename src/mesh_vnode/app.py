"""Wiring: one object that owns the database, the upstream link and the server."""

from __future__ import annotations

import asyncio
import logging

from . import protocol as proto
from .config import Settings
from .db import Database
from .upstream import Upstream
from .vnode_server import VNodeServer

logger = logging.getLogger(__name__)


class VNodeApp:
    def __init__(self, settings: Settings, *, cli_upstream: bool = False) -> None:
        self.settings = settings
        self.db = Database(settings.db_path)
        # Where the node address comes from, strongest first: --node on the
        # command line, then the web UI's setting, then VNODE_UPSTREAM_HOST.
        self.cli_upstream = cli_upstream
        self.upstream = Upstream(settings, self.db, *self.upstream_target())
        self.server = VNodeServer(settings, self.db, self.upstream)
        self.apply_admin_pref()
        self.apply_forward_pref()
        self._prune_task: asyncio.Task | None = None

    @property
    def upstream_fallback(self) -> tuple[str, int]:
        """The address without a web UI setting."""
        return self.settings.upstream_host, self.settings.upstream_port

    @property
    def upstream_fallback_set(self) -> bool:
        """Whether VNODE_UPSTREAM_HOST was given, or the fallback is only the default."""
        return "upstream_host" in self.settings.model_fields_set

    def apply_admin_pref(self) -> None:
        """Let apps change the node's settings if the web UI says so, else
        whatever VNODE_ALLOW_ADMIN says."""
        stored = self.db.prefs().get("allow_admin")
        self.server.allow_admin = self.settings.allow_admin if stored is None else bool(stored)

    def apply_forward_pref(self) -> None:
        """What live traffic apps get; everything unless the web UI says less."""
        stored = self.db.prefs().get("app_forward") or {}
        self.server.forward_policy = {c: stored.get(c, "all") for c in proto.FORWARD_CATEGORIES}

    def upstream_target(self) -> tuple[str, int]:
        """The node address the web UI's settings call for."""
        host, port = self.upstream_fallback
        if self.cli_upstream:
            return host, port
        prefs = self.db.prefs()
        return prefs.get("upstream_host") or host, prefs.get("upstream_port") or port

    async def start(self) -> None:
        await self.server.start()
        self.upstream.start()
        self._prune_task = asyncio.create_task(self._prune_loop())
        logger.info(
            "vnode: up. node %s:%s -> virtual node :%s, replay=%s",
            self.upstream.host,
            self.upstream.port,
            self.settings.listen_port,
            self.settings.replay_mode,
        )

    async def stop(self) -> None:
        if self._prune_task is not None:
            self._prune_task.cancel()
        await self.server.stop()
        self.upstream.stop()
        self.db.close()

    async def _prune_loop(self) -> None:
        while True:
            try:
                removed = await asyncio.to_thread(self.db.prune, self.settings.retention_days)
                if removed:
                    logger.info(
                        "vnode: pruned %d packets older than %d days",
                        removed,
                        self.settings.retention_days,
                    )
            except Exception:
                logger.exception("vnode: prune failed")
            await asyncio.sleep(6 * 3600)
