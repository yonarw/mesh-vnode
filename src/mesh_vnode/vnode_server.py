"""Downstream half: the asyncio TCP server the phone app connects to.

To the app this looks like a node on TCP. It answers `want_config_id` with the
handshake captured from the real node, then hands over the text messages that
client has not seen yet, then goes transparent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from meshtastic.protobuf import mesh_pb2

from . import protocol as proto
from .config import Settings
from .db import Database
from .framing import FrameDecoder, encode_frame
from .logging_setup import TRACE, hexdump, trace
from .upstream import FrameContext, Upstream

logger = logging.getLogger(__name__)


class ClientSession:
    def __init__(self, cid: int, key: str, peer: str, writer: asyncio.StreamWriter) -> None:
        self.cid = cid
        self.key = key
        self.peer = peer
        self.writer = writer
        self.decoder = FrameDecoder()
        self.connected_at = time.time()
        self.last_activity = time.time()
        self.live = False  # set once the handshake (and replay) is done
        # Live frames that arrived mid-handshake, with their store seq so the
        # flush can skip anything the replay already covered.
        self.pending: list[tuple[bytes, int | None]] = []
        self.last_config_id: int | None = None
        self.last_config_at: float = 0.0
        self.cursor: int = 0
        self.replayed: int = 0
        self.sent: int = 0
        self.received: int = 0

    def info(self) -> dict[str, Any]:
        return {
            "id": self.cid,
            "key": self.key,
            "peer": self.peer,
            "connected_at": self.connected_at,
            "last_activity": self.last_activity,
            "live": self.live,
            "cursor": self.cursor,
            "replayed": self.replayed,
            "frames_sent": self.sent,
            "frames_received": self.received,
        }


class VNodeServer:
    CONFIG_COOLDOWN_S = 5.0
    MAX_PENDING = 500

    def __init__(self, settings: Settings, db: Database, upstream: Upstream) -> None:
        self.settings = settings
        self.db = db
        self.upstream = upstream
        self.clients: dict[int, ClientSession] = {}
        self._next_id = 1
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listeners: list[Any] = []  # web UI push callbacks
        self._reaper: asyncio.Task | None = None
        self._handlers: set[asyncio.Task] = set()
        # What live traffic apps get, per category (protocol.FORWARD_CATEGORIES):
        # "all", "favorites" or "none". Set from the web UI's settings.
        self.forward_policy: dict[str, str] = {}

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.upstream.add_raw_sink(self._upstream_sink)
        self._server = await asyncio.start_server(
            self._handle_client, self.settings.listen_host, self.settings.listen_port
        )
        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets or [])
        logger.info("vnode: virtual node listening on %s", addrs)
        self._reaper = asyncio.create_task(self._idle_reaper())

    async def stop(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
        if self._server is not None:
            self._server.close()
        # Since Python 3.12 wait_closed() also waits for every open connection.
        self.disconnect_all()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        if self._server is not None:
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    def add_listener(self, cb) -> None:
        """Register a callback for web-UI events (called on the event loop)."""
        self._listeners.append(cb)

    def remove_listener(self, cb) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(cb)

    def _notify(self, event: str, payload: dict[str, Any]) -> None:
        for cb in list(self._listeners):
            try:
                cb(event, payload)
            except Exception:
                logger.exception("vnode: listener failed")

    # ------------------------------------------------------ upstream fan-out

    def _upstream_sink(self, raw: bytes, ctx: FrameContext) -> None:
        """Called on the upstream reader thread - hop to the event loop."""
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._fanout, raw, ctx.seq, ctx.kind)

    def _wanted_by_apps(self, raw: bytes) -> bool:
        """Does this live frame pass the "what to send to apps" settings?"""
        if not self.forward_policy or all(v == "all" for v in self.forward_policy.values()):
            return True
        try:
            fr = proto.parse_from_radio(raw)
        except Exception:
            return True
        category = proto.forward_category(fr, self.upstream.my_node_num)
        mode = self.forward_policy.get(category, "all") if category else "all"
        if mode == "all":
            return True
        if mode == "favorites":
            favorites = getattr(self.upstream, "favorites", set())
            return fr.packet.__getattribute__("from") in favorites
        return False

    def _fanout(self, raw: bytes, seq: int | None, kind: str | None) -> None:
        if self.clients and self._wanted_by_apps(raw):
            for client in list(self.clients.values()):
                self._deliver(client, raw, seq)
        # Only stored packets (text) are worth waking the web UI for; on a busy
        # mesh every position and telemetry packet would refetch every view.
        if seq is not None:
            self._notify("packet", {"seq": seq})

    def mirror_outgoing(self, pkt, seq: int, exclude: int | None = None) -> None:
        """Show every connected app a message we sent, as it happens.

        The node does not echo our own transmissions, so a message sent from
        one app, or from the web UI, would otherwise only reach the others
        through the replay on their next connect. Call on the event loop.
        """
        mirror = proto.wrap_packet(pkt, override_from=self.upstream.my_node_num)
        for other in list(self.clients.values()):
            if other.cid != exclude:
                self._deliver(other, mirror, seq)
        self._notify("packet", {"seq": seq})

    def _deliver(self, client: ClientSession, raw: bytes, seq: int | None) -> None:
        if not client.live:
            # A client that connects and never asks for config would otherwise
            # grow this without bound. The dropped frames are still in the store
            # and come back through the cursor replay.
            # Dropping past the cap is safe: anything with a seq is in the
            # store and comes back through the cursor replay a moment later.
            if len(client.pending) < self.MAX_PENDING:
                client.pending.append((raw, seq))
            return
        self._write(client, raw)
        if seq is not None and seq > client.cursor:
            client.cursor = seq
            self.db.set_client_cursor(client.key, seq)

    def _write(self, client: ClientSession, payload: bytes) -> None:
        try:
            client.writer.write(encode_frame(payload))
            client.sent += 1
        except Exception as exc:
            # A failed write is exactly why a phone "misses" a message, so this
            # is a warning rather than a debug line.
            logger.warning("vnode: write to client %s failed: %s", client.peer, exc)
            return
        if logger.isEnabledFor(TRACE):
            try:
                desc = proto.describe_from_radio(proto.parse_from_radio(payload))
            except Exception:
                desc = "unparseable"
            trace(logger, "-> client %s  %s | %s", client.peer, desc, hexdump(payload))

    # -------------------------------------------------------- client handling

    def _client_key(self, peer_host: str, peer_port: int) -> str:
        mode = self.settings.client_key_mode
        if mode == "shared":
            return "shared"
        if mode == "ip_port":
            return f"{peer_host}:{peer_port}"
        return peer_host

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        sock = writer.get_extra_info("peername")
        host, port = (sock[0], sock[1]) if sock else ("unknown", 0)
        cid = self._next_id
        self._next_id += 1
        key = self._client_key(host, port)
        client = ClientSession(cid, key, f"{host}:{port}", writer)
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
            task.add_done_callback(self._handlers.discard)

        row = self.db.get_or_create_client(key, label=host)
        client.cursor = int(row["last_seq"])
        self.clients[cid] = client

        logger.info(
            "vnode: client %s connected from %s (cursor=%d)", cid, client.peer, client.cursor
        )
        self.db.log_event(
            "info", "client", f"connected {client.peer} key={key} cursor={client.cursor}"
        )
        self._notify("client", {"action": "connect", **client.info()})

        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                client.last_activity = time.time()
                for payload in client.decoder.feed(data):
                    client.received += 1
                    if logger.isEnabledFor(TRACE):
                        try:
                            desc = proto.describe_to_radio(proto.parse_to_radio(payload))
                        except Exception:
                            desc = "unparseable"
                        trace(logger, "<- client %s  %s | %s", client.peer, desc, hexdump(payload))
                    await self._handle_to_radio(client, payload)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except Exception:
            logger.exception("vnode: client %s failed", cid)
        finally:
            self.clients.pop(cid, None)
            with contextlib.suppress(Exception):
                writer.close()
            logger.info(
                "vnode: client %s disconnected (sent=%d recv=%d)", cid, client.sent, client.received
            )
            self.db.log_event("info", "client", f"disconnected {client.peer}")
            self._notify("client", {"action": "disconnect", **client.info()})

    async def _handle_to_radio(self, client: ClientSession, payload: bytes) -> None:
        try:
            tr = proto.parse_to_radio(payload)
        except Exception as exc:
            # Keep the bytes: an undecodable frame cannot be diagnosed without them.
            logger.warning(
                "vnode: undecodable ToRadio from %s: %s | %s",
                client.peer,
                exc,
                hexdump(payload, limit=64),
            )
            return

        which = tr.WhichOneof("payload_variant")

        if which == "want_config_id":
            nonce = tr.want_config_id
            now = time.time()
            if (
                client.last_config_id == nonce
                and now - client.last_config_at < self.CONFIG_COOLDOWN_S
            ):
                logger.debug("vnode: ignoring repeat want_config_id %s from %s", nonce, client.peer)
                return
            client.last_config_id = nonce
            client.last_config_at = now
            await self._send_config(client, nonce)
            return

        if which == "heartbeat":
            # iOS drops the connection if a heartbeat goes unanswered. The real
            # node replies with whatever it has; a queue status is enough.
            self._write(client, proto.build_queue_status())
            await self._drain(client)
            return

        if which == "disconnect":
            # Handled locally: the shared upstream session must survive a phone
            # closing its app.
            logger.info("vnode: client %s requested disconnect", client.cid)
            return

        if which == "packet":
            await self._forward_packet(client, tr, payload)
            return

        # xmodem, mqttClientProxyMessage, anything new: pass it through.
        await self._send_upstream(payload)

    async def _forward_packet(self, client: ClientSession, tr, payload: bytes) -> None:
        pkt = tr.packet
        portnum = int(pkt.decoded.portnum) if pkt.HasField("decoded") else 0

        if portnum == proto.PORT_ADMIN:
            await self._forward_admin(client, pkt, payload)
            return

        # Anything a person did is worth an INFO line: these are rare and they
        # are what you compare against "did it arrive?".
        logger.info("vnode: %s sent %s", client.peer, proto.describe_packet(pkt))

        await self._note_app_request(client, pkt, portnum)

        out = self._apply_from_zero_fix(client, payload, pkt)

        if self.settings.store_outgoing:
            seq = await asyncio.to_thread(self.upstream.store_client_packet, pkt, client.key)
            if seq is None:
                logger.debug(
                    "vnode: outgoing packet id=%#010x was already stored, not mirrored",
                    pkt.id,
                )
            else:
                client.cursor = max(client.cursor, seq)
                self.db.set_client_cursor(client.key, seq)
                self.mirror_outgoing(pkt, seq, exclude=client.cid)

        await self._send_upstream(out)

    async def _note_app_request(self, client: ClientSession, pkt, portnum: int) -> None:
        """Record a traceroute or a position/telemetry/node info request an app
        made through us.

        Being the connection the app talks through is what makes this possible:
        the answer comes back from the mesh addressed to our node, so it lands
        in the same exchange row as one the web UI asked for.
        """
        kind = proto.exchange_kind(portnum)
        if kind is None or not proto.is_request(pkt) or pkt.to == proto.BROADCAST_NUM:
            return

        def store() -> dict[str, Any] | None:
            row_id = self.db.log_exchange(
                kind=kind,
                node_num=pkt.to,
                direction="out",
                status="sent",
                channel=pkt.channel,
                packet_id=pkt.id,
                origin=client.key,
            )
            row = self.db.exchange(row_id)
            return dict(row) if row is not None else None

        row = await asyncio.to_thread(store)
        if row is not None:
            self._notify("exchange", row)

    async def _forward_admin(self, client: ClientSession, pkt, payload: bytes) -> None:
        """Admin messages: reads and node flags pass, anything else is dropped.

        Forwarded byte-for-byte: no sender fix-up (the node treats from=0 as its
        own phone, which is what an admin request to it should look like) and
        nothing stored, since an admin exchange is not chat.
        """
        variant, desc = proto.describe_admin(pkt)
        flag_change = proto.safe_admin_change(pkt)
        read_only = variant in proto.READ_ONLY_ADMIN_VARIANTS
        target = proto.node_id(pkt.to) if pkt.to else "its node"

        if not self.settings.allow_admin:
            allowed = flag_change is not None or (read_only and self.settings.allow_admin_reads)
            if not allowed:
                routine = variant in proto.ROUTINE_BLOCKED_ADMIN_VARIANTS
                logger.log(
                    logging.DEBUG if routine else logging.WARNING,
                    "vnode: blocked admin %s from %s to %s - only read requests and "
                    "favourite/ignore flags pass (VNODE_ALLOW_ADMIN=true lets everything through)",
                    desc,
                    client.peer,
                    target,
                )
                if variant is None:
                    logger.warning(
                        "vnode: blocked admin payload | %s", hexdump(pkt.decoded.payload, limit=64)
                    )
                if not routine:
                    self.db.log_event("warn", "client", f"blocked admin {desc} from {client.peer}")
                # Say no, rather than leave the app waiting for an answer.
                my_num = self.upstream.my_node_num
                if my_num and pkt.id:
                    self._write(
                        client,
                        proto.build_routing_error(
                            my_num, pkt.id, mesh_pb2.Routing.Error.NOT_AUTHORIZED
                        ),
                    )
                return

        logger.info("vnode: %s -> %s: admin %s", client.peer, target, desc)
        if not await self._send_upstream(payload):
            return

        if not read_only and flag_change is None:
            # The app changed a setting: our stored copy of the config is now
            # stale, and it is what the next app to connect gets.
            self.upstream.refresh_config_soon()

        if flag_change is not None:
            flag_variant, node_num = flag_change
            flags = {
                "set_favorite_node": {"is_favorite": True},
                "remove_favorite_node": {"is_favorite": False},
                "set_ignored_node": {"is_ignored": True},
                "remove_ignored_node": {"is_ignored": False},
            }[flag_variant]
            await asyncio.to_thread(self.upstream.note_node_flags, node_num, **flags)
            self._notify("nodes", {"node_num": node_num, **flags})

    def _apply_from_zero_fix(self, client: ClientSession, payload: bytes, pkt) -> bytes:
        """Stamp our node number into a packet the client sent with from=0.

        Android builds PKI direct messages that way, expecting its own node to
        fill the field in; through a proxy that never happens and the node drops
        them. Stamping is exactly what the node would have done, so the message
        stays sealed to its recipient - there is no variant of this worth
        offering, and nothing here weakens the encryption.
        """
        out = proto.fill_from_if_zero(payload, self.upstream.my_node_num or 0)
        if out is not payload:
            logger.debug(
                "vnode: stamped node number into packet id=%#010x from %s (encryption untouched)",
                pkt.id,
                client.peer,
            )
        return out

    async def _send_upstream(self, payload: bytes) -> bool:
        if not self.upstream.connected.is_set():
            # Worth its own message: the packet is gone, and "the node was down"
            # is not obvious from a generic send failure.
            logger.warning(
                "vnode: dropping a packet for the node - upstream is not connected (%s)",
                self.upstream.last_error or "not connected yet",
            )
            self.db.log_event("warn", "upstream", "packet dropped: upstream not connected")
            return False
        try:
            await asyncio.to_thread(self.upstream.send_to_radio_bytes, payload)
            trace(logger, "-> node   %s", hexdump(payload))
            return True
        except Exception as exc:
            logger.warning("vnode: upstream send failed: %s", exc)
            self.db.log_event("warn", "upstream", f"send failed: {exc}")
            return False

    # --------------------------------------------------------- config replay

    async def _send_config(self, client: ClientSession, nonce: int) -> None:
        """Replay the captured handshake, in the order the firmware uses.

        PhoneAPI.cpp says the client apps assume this sequence, so the frames go
        out in the order they were captured, which is that sequence. Two nonces
        are special and ask for a subset.
        """
        client.live = False
        frames = self.db.config_frames()
        if not any(f["kind"] == "my_info" for f in frames):
            logger.warning("vnode: no captured config yet, cannot serve %s", client.peer)
            self.db.log_event("warn", "client", f"config requested by {client.peer} before capture")
            return

        only_config = nonce == proto.NONCE_ONLY_CONFIG
        only_db = nonce == proto.NONCE_ONLY_DB

        # What the firmware sends per nonce (PhoneAPI.cpp, 2.8.1):
        # https://github.com/meshtastic/firmware/blob/3468af94aa0f79e93c9bf041244bf230161fc704/src/mesh/PhoneAPI.cpp
        # The capture is a full handshake in firmware order, so each phase is a filter over
        # it that keeps that order:
        #   full   my_info, ui config, own node, metadata, region presets,
        #          channels, config, module config, other nodes, files
        #   69420  the same without the other nodes
        #   69421  own node, then the other nodes - no my_info, nothing else
        my_num = self._captured_node_num(frames)

        def is_other_node(f) -> bool:
            return f["kind"] == "node_info" and f["key"] != f"node_info:{my_num}"

        if only_db:
            own = [f for f in frames if f["key"] == f"node_info:{my_num}"]
            selected = own + [f for f in frames if is_other_node(f)]
        elif only_config:
            selected = [f for f in frames if not is_other_node(f)]
        else:
            selected = list(frames)

        for row in selected:
            self._write(client, row["raw"])
        await self._drain(client)
        logger.info(
            "vnode: sent %d config frames to client %s (nonce=%s)", len(selected), client.cid, nonce
        )

        replay_rows = self._replay_rows(client) if self.settings.replay_enabled else []

        # After config_complete, which is what every client tested handles.
        self._write(client, proto.build_config_complete(nonce or 1))
        await self._drain(client)
        if replay_rows:
            await self._replay(client, replay_rows)

        # Anything that arrived from the mesh while we were talking. Frames the
        # replay already covered are skipped, or the client would see them twice.
        pending, client.pending = client.pending, []
        for raw, seq in pending:
            if seq is not None and seq <= client.cursor:
                continue
            self._write(client, raw)
            if seq is not None:
                client.cursor = max(client.cursor, seq)
        self.db.set_client_cursor(client.key, client.cursor)
        client.live = True
        await self._drain(client)
        self._notify("client", {"action": "configured", **client.info()})

    @staticmethod
    def _captured_node_num(frames) -> int | None:
        for f in frames:
            if f["kind"] == "my_info":
                return proto.parse_from_radio(f["raw"]).my_info.my_node_num
        return None

    def _replay_rows(self, client: ClientSession) -> list:
        s = self.settings
        min_rx = int(time.time()) - s.replay_max_age_days * 86400 if s.replay_max_age_days else None
        exclude = client.key if s.replay_skip_own else None
        filters = {
            "min_rx_time": min_rx,
            "portnums": proto.REPLAY_PORTNUMS,
            "exclude_origin": exclude,
        }
        # A backlog longer than the limit must yield the NEWEST slice, not the
        # oldest. Taking the oldest would advance the cursor past the tail and
        # the messages in between would never be delivered at all.
        backlog = self.db.count_after(client.cursor, **filters)
        offset = max(0, backlog - s.replay_limit)
        if offset:
            logger.warning(
                "vnode: client %s is %d messages behind, replaying the newest %d",
                client.cid,
                backlog,
                s.replay_limit,
            )
            self.db.log_event(
                "warn",
                "client",
                f"{client.peer} was {backlog} behind, skipped {offset} over the replay limit",
            )
        return self.db.packets_after(client.cursor, limit=s.replay_limit, offset=offset, **filters)

    async def _replay(self, client: ClientSession, rows: list) -> None:
        pace = self.settings.replay_pace_ms / 1000.0
        last_seq = client.cursor
        if logger.isEnabledFor(logging.DEBUG) and rows:
            # The list, not just the count: "did it show the same message twice"
            # is answered by comparing packet ids across two connects.
            logger.debug(
                "vnode: replaying to client %s: %s",
                client.cid,
                ", ".join(f"seq={r['seq']} id={r['packet_id']:#010x}" for r in rows),
            )
        for row in rows:
            self._write(client, row["raw"])
            last_seq = max(last_seq, int(row["seq"]))
            if pace:
                await asyncio.sleep(pace)
        await self._drain(client)
        client.replayed += len(rows)
        client.cursor = last_seq
        self.db.set_client_cursor(client.key, last_seq, replayed=len(rows))
        logger.info(
            "vnode: replayed %d stored messages to client %s (cursor now %d)",
            len(rows),
            client.cid,
            last_seq,
        )
        self.db.log_event("info", "client", f"replayed {len(rows)} to {client.peer}")

    async def _drain(self, client: ClientSession) -> None:
        with contextlib.suppress(Exception):
            await client.writer.drain()

    def disconnect_all(self) -> int:
        """Drop every connected app, e.g. after the store was cleared: they
        reconnect by themselves and start from a clean slate. Taken out of
        the client list at once, so nothing more is delivered to them or
        moves their cursors. Call on the event loop."""
        dropped = list(self.clients.values())
        for client in dropped:
            self.clients.pop(client.cid, None)
            with contextlib.suppress(Exception):
                client.writer.close()
        return len(dropped)

    # ------------------------------------------------------------ maintenance

    async def _idle_reaper(self) -> None:
        timeout = self.settings.client_idle_timeout_s
        while timeout > 0:
            await asyncio.sleep(60)
            now = time.time()
            for client in list(self.clients.values()):
                if now - client.last_activity > timeout:
                    logger.info("vnode: dropping idle client %s", client.cid)
                    with contextlib.suppress(Exception):
                        client.writer.close()

    def status(self) -> dict[str, Any]:
        return {
            "listen": f"{self.settings.listen_host}:{self.settings.listen_port}",
            "clients": [c.info() for c in self.clients.values()],
            "replay_mode": self.settings.replay_mode,
            "client_key_mode": self.settings.client_key_mode,
        }
