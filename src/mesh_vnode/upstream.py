"""Upstream half: the single, permanent connection to the real node.

This subclasses the `meshtastic` package's TCPInterface rather than re-speaking
the protocol, so reconnects, heartbeats and the config handshake come for free.
The one override is `_handleFromRadio`, which hands us the raw FromRadio bytes
before the library parses them. Those exact bytes are what gets stored and later
replayed, which is the whole point: a replayed packet is byte-identical to what
the node sent, so hop counts and PKI fields stay truthful.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from meshtastic.protobuf import config_pb2, mesh_pb2
from meshtastic.tcp_interface import TCPInterface

from . import protocol as proto
from .config import Settings
from .db import Database
from .logging_setup import TRACE, hexdump, trace

logger = logging.getLogger(__name__)

RawSink = Callable[[bytes, "FrameContext"], None]
StateSink = Callable[[str, str], None]


def is_own_virtual_node(host: str, port: int, listen_port: int) -> bool:
    """Would connecting to host:port reach this service's own virtual node?

    That makes a loop - everything we send ourselves comes back as new input -
    and pins a CPU. An address is ours when it is loopback or unspecified, or
    when a socket can be bound to it: only addresses on this machine can.
    A name that does not resolve is not ours (the connect will say why).
    """
    if port != listen_port:
        return False
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for family, _, _, _, addr in infos:
        ip = ipaddress.ip_address(addr[0].split("%")[0])
        if ip.is_loopback or ip.is_unspecified:
            return True
        with contextlib.suppress(OSError), socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((addr[0], 0))
            return True
    return False


class FrameContext:
    """What we know about one FromRadio frame when it reaches the sinks."""

    __slots__ = ("during_config", "kind", "seq")

    def __init__(self, during_config: bool, seq: int | None, kind: str | None) -> None:
        self.during_config = during_config
        self.seq = seq
        self.kind = kind


class _Interface(TCPInterface):
    """TCPInterface with a tap on the raw frame stream."""

    def __init__(self, hostname: str, port: int, owner: Upstream, timeout: int = 60) -> None:
        self._owner = owner
        self._config_ord = 0
        self._capturing = True
        self._captured: list[tuple[str, str, int, bytes]] = []
        super().__init__(hostname, portNumber=port, connectNow=True, timeout=timeout)

    def myConnect(self) -> None:
        super().myConnect()
        # Without keepalive a node that loses power leaves a half-open socket:
        # recv() blocks forever and nothing notices until the library's
        # five-minute heartbeat fails. With it the kernel gives up after about
        # two minutes of silence, recv() raises, and the supervisor reconnects.
        sock = self.socket
        if sock is None:
            return
        with contextlib.suppress(OSError, AttributeError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 15)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)

    def reader_alive(self) -> bool:
        """False once the library's reader thread has given up.

        On an error such as ECONNRESET the reader logs "terminating meshtastic
        reader" and exits, but leaves `socket` set, so nothing else looks
        dead. Until the next heartbeat write failed - up to five minutes later -
        no frames were read at all.
        """
        rx = getattr(self, "_rxThread", None)
        return rx is None or rx.is_alive()

    # The library resets its model here on connect and on reconnect, so it is
    # the right place to start a fresh capture of the handshake.
    def _startConfig(self):
        self._capturing = True
        self._config_ord = 0
        self._captured = []
        return super()._startConfig()

    def _handleConfigComplete(self) -> None:
        self._capturing = False
        self._owner._on_config_captured(self._captured)
        return super()._handleConfigComplete()

    def _sendToRadioImpl(self, toRadio) -> None:
        # Everything the library itself sends - heartbeats, config requests,
        # admin messages - passes through here, so this is where it is traced.
        if logger.isEnabledFor(TRACE):
            trace(logger, "-> node   %s", proto.describe_to_radio(toRadio))
        return super()._sendToRadioImpl(toRadio)

    def _handleFromRadio(self, fromRadioBytes):
        try:
            self._owner._on_raw_frame(bytes(fromRadioBytes), self)
        except Exception:
            # Include the frame: a parse or storage failure is undiagnosable
            # without the bytes that caused it.
            logger.exception(
                "vnode: error handling upstream frame | %s", hexdump(bytes(fromRadioBytes), 64)
            )
        return super()._handleFromRadio(fromRadioBytes)


class Upstream:
    """Owns the interface, the reconnect supervisor and the raw-frame fan-out."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.iface: _Interface | None = None
        self.connected = threading.Event()
        self.last_error: str | None = None
        self.connected_since: float | None = None
        self.config_captured_at: float | None = None

        # Nodes whose telemetry is worth keeping besides our own. Read from the
        # node's favourites; refreshed whenever they change.
        self._favorites: set[int] = db.favorite_nums()

        self._raw_sinks: list[RawSink] = []
        self._state_sinks: list[StateSink] = []
        self._event_sinks: list[Callable[[str, dict], None]] = []
        # Status updates that arrived before their message was stored. The web
        # UI sends through the library, which transmits first; the node's
        # "queued" reply can beat the insert by milliseconds.
        self._early_status: dict[int, list[tuple[str, str | None, str, str, dict]]] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Set to cut the current connection short and reconnect at once, e.g.
        # after the node's address was changed in the web UI.
        self._kick = threading.Event()
        self._refresh_lock = threading.Lock()
        self._refresh_timer: threading.Timer | None = None

    # ------------------------------------------------------------- lifecycle

    def add_raw_sink(self, sink: RawSink) -> None:
        self._raw_sinks.append(sink)

    def add_state_sink(self, sink: StateSink) -> None:
        self._state_sinks.append(sink)

    def add_event_sink(self, sink: Callable[[str, dict], None]) -> None:
        """Events for the web UI, called on the upstream thread."""
        self._event_sinks.append(sink)

    def _emit_event(self, event: str, payload: dict) -> None:
        for sink in self._event_sinks:
            try:
                sink(event, payload)
            except Exception:
                logger.exception("vnode: event sink failed")

    def _set_status(
        self,
        packet_id: int,
        status: str,
        detail: str | None,
        why: str,
        event: str,
        fields: dict | None = None,
    ) -> None:
        """Record what the node said about one of our messages, and advance
        its status if that is progress. `event` and `fields` go to the
        delivery log whether or not the status moves: a second implicit ack
        changes nothing, but it is still evidence worth showing."""
        my_num = self.my_node_num
        if not my_num:
            return
        fields = {"ts": int(time.time()), **(fields or {})}
        if self.db.find_own_packet(packet_id, my_num) is None:
            # Not stored yet - or not ours at all. Keep it briefly either way.
            self._early_status.setdefault(packet_id, []).append(
                (status, detail, why, event, fields)
            )
            while len(self._early_status) > 64:
                self._early_status.pop(next(iter(self._early_status)))
            return
        self.db.log_delivery(packet_id, my_num, event, **fields)
        changed = self.db.update_status(packet_id, my_num, status, detail)
        if changed:
            logger.info(
                "vnode: message id=%#010x is now %s%s (%s)",
                packet_id,
                status,
                f" ({detail})" if detail else "",
                why,
            )
            self._emit_event("status", changed)

    def _apply_early_status(self, packet_id: int) -> None:
        for status, detail, why, event, fields in self._early_status.pop(packet_id, []):
            self._set_status(packet_id, status, detail, why, event, fields)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._supervise, name="vnode-upstream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()
        iface = self.iface
        if iface is not None:
            with contextlib.suppress(Exception):
                iface.close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _supervise(self) -> None:
        """Connect, and keep trying. The library reconnects a live socket by
        itself; this loop only covers the case where it never came up or gave up.
        """
        backoff = 2
        while not self._stop.is_set():
            host, port = self.settings.upstream_host, self.settings.upstream_port
            if is_own_virtual_node(host, port, self.settings.listen_port):
                self.last_error = (
                    f"{host}:{port} is this service's own virtual node, not the real node"
                )
                logger.error("vnode: refusing to connect: %s", self.last_error)
                self._emit_state("error", self.last_error)
                self._kick.wait(30)
                self._kick.clear()
                continue
            try:
                self._emit_state(
                    "connecting", f"{self.settings.upstream_host}:{self.settings.upstream_port}"
                )
                self.iface = _Interface(
                    self.settings.upstream_host, self.settings.upstream_port, self
                )
                self.connected.set()
                self.connected_since = time.time()
                self.last_error = None
                backoff = 2
                self._emit_state("connected", proto.node_id(self.my_node_num or 0))
                # Park here until the socket dies or we are told to stop.
                while not self._stop.is_set() and not self._kick.is_set():
                    if self.iface is None or self.iface.socket is None:
                        break
                    if not self.iface.reader_alive():
                        logger.warning("vnode: upstream reader stopped, reconnecting")
                        self.last_error = "connection lost"
                        break
                    time.sleep(1)
            except Exception as exc:  # connection refused, DNS, timeout, ...
                self.last_error = str(exc)
                target = f"{self.settings.upstream_host}:{self.settings.upstream_port}"
                hint = ""
                if "Name or service not known" in str(exc) or "nodename nor servname" in str(exc):
                    hint = " - hostname does not resolve; use the node's IP (--node <ip>)"
                elif "refused" in str(exc).lower():
                    hint = (
                        " - is another client (MeshMonitor, the app over WiFi) "
                        "holding the node's single TCP slot?"
                    )
                logger.warning("vnode: cannot connect to node %s: %s%s", target, exc, hint)
                self._emit_state("error", str(exc))
            finally:
                self.connected.clear()
                self.connected_since = None
                if self.iface is not None:
                    with contextlib.suppress(Exception):
                        self.iface.close()
                    self.iface = None
            if self._stop.is_set():
                break
            if self._kick.is_set():
                # Asked to reconnect (new address): go straight away.
                self._kick.clear()
                backoff = 2
                continue
            self._emit_state("reconnecting", f"retry in {backoff}s")
            self._kick.wait(backoff)
            self._kick.clear()
            backoff = min(backoff * 2, 60)

    def refresh_config_soon(self, delay: float = 8.0) -> None:
        """Re-capture the handshake shortly, after an app changed the node's
        settings.

        Apps get the config from our stored copy, so without this they would
        see the old settings until the link happened to reconnect. Many
        changes reboot the node, which reconnects anyway; channel and owner
        changes do not. Debounced: an app saves a settings page as a burst of
        admin messages, and one refresh after the last is enough.
        """
        with self._refresh_lock:
            if self._refresh_timer is not None:
                self._refresh_timer.cancel()
            self._refresh_timer = threading.Timer(delay, self._refresh_now)
            self._refresh_timer.daemon = True
            self._refresh_timer.start()

    def _refresh_now(self) -> None:
        if self.connected.is_set():
            logger.info("vnode: node settings changed, reconnecting to re-read them")
            self._kick.set()

    def recapture(self) -> None:
        """Reconnect now, which re-reads the node's config and node list."""
        self._early_status.clear()
        self._kick.set()

    def retarget(self, host: str, port: int) -> None:
        """Point the link at a different node and reconnect now."""
        if (host, port) == (self.settings.upstream_host, self.settings.upstream_port):
            return
        logger.info("vnode: upstream changed to %s:%s", host, port)
        self.settings.upstream_host = host
        self.settings.upstream_port = port
        self._kick.set()

    # ------------------------------------------------------------- accessors

    @property
    def my_node_num(self) -> int | None:
        iface = self.iface
        if iface is not None and iface.myInfo is not None:
            return iface.myInfo.my_node_num
        row = self.db.config_frames(kinds=["my_info"])
        if row:
            fr = proto.parse_from_radio(row[0]["raw"])
            return fr.my_info.my_node_num
        return None

    def status(self) -> dict:
        iface = self.iface
        return {
            "connected": bool(self.connected.is_set()),
            "host": self.settings.upstream_host,
            "port": self.settings.upstream_port,
            "connected_since": self.connected_since,
            "config_captured_at": self.config_captured_at,
            "config_frames": len(self.db.config_frames()),
            "my_node_num": self.my_node_num,
            "my_node_id": proto.node_id(self.my_node_num) if self.my_node_num else None,
            "firmware": (iface.metadata.firmware_version if iface and iface.metadata else None),
            "last_error": self.last_error,
        }

    def config_ready(self) -> bool:
        """True once a full handshake has been captured (now or in a past run)."""
        frames = self.db.config_frames()
        return any(f["kind"] == "my_info" for f in frames)

    # -------------------------------------------------------------- send path

    def send_to_radio_bytes(self, to_radio_bytes: bytes) -> None:
        """Forward a client's ToRadio upstream. Blocking - call from a thread."""
        iface = self.iface
        if iface is None:
            raise RuntimeError("upstream not connected")
        tr = proto.parse_to_radio(to_radio_bytes)
        iface._sendToRadio(tr)

    def send_text(
        self,
        text: str,
        *,
        destination: int | str = proto.BROADCAST_NUM,
        channel_index: int = 0,
        want_ack: bool = False,
        reply_id: int | None = None,
        emoji: bool = False,
    ):
        iface = self.iface
        if iface is None:
            raise RuntimeError("upstream not connected")
        # A DM goes out the way the Android app sends one: flagged for PKI with
        # the recipient's key. The node would seal it anyway, but only the flag
        # makes our stored copy say so - and the app files PKI DMs in a
        # different conversation from plain ones.
        public_key = None
        if isinstance(destination, int) and destination != proto.BROADCAST_NUM:
            my_num = self.my_node_num
            if my_num and self.db.public_key(my_num):
                public_key = self.db.public_key(destination)
        if not emoji:
            return iface.sendData(
                text.encode("utf-8"),
                destinationId=destination,
                portNum=proto.PORT_TEXT,
                wantAck=want_ack,
                channelIndex=channel_index,
                pkiEncrypted=public_key is not None,
                publicKey=public_key,
                replyId=reply_id,
            )
        # A reaction is an ordinary text packet whose payload is the emoji, with
        # `emoji` set and `reply_id` naming the message it belongs to.
        # meshtastic-python's sendData carries reply_id but has no emoji
        # parameter, so build the packet exactly as sendData would - same
        # defaults, same id source - and hand it to the same _sendPacket.
        packet = mesh_pb2.MeshPacket()
        packet.channel = channel_index
        packet.decoded.payload = text.encode("utf-8")
        packet.decoded.portnum = proto.PORT_TEXT
        packet.decoded.emoji = 1
        if reply_id:
            packet.decoded.reply_id = reply_id
        packet.id = iface._generatePacketId()
        packet.priority = mesh_pb2.MeshPacket.Priority.RELIABLE
        return iface._sendPacket(
            packet,
            destination,
            wantAck=want_ack,
            pkiEncrypted=public_key is not None,
            publicKey=public_key,
        )

    def send_request(
        self,
        kind: str,
        node_num: int,
        *,
        channel_index: int = 0,
        origin: str = "webui",
    ) -> dict[str, Any]:
        """Ask one node for something: a traceroute, or its position, telemetry
        or node info.

        The request goes out with want_response set and nothing waits for the
        answer - it arrives from the mesh like any other packet, minutes later
        for a traceroute across several hops, and is matched back to this row
        by its request_id. Blocking only for as long as the send takes; call
        from a thread. Returns the stored row.
        """
        iface = self.iface
        if iface is None:
            raise RuntimeError("upstream not connected")
        port = proto.EXCHANGE_PORTS.get(kind)
        if port is None:
            raise ValueError(f"unknown exchange kind: {kind}")
        my_num = self.my_node_num
        me = self.db.node(my_num) if my_num else None
        packet = iface.sendData(
            proto.request_payload(kind, me=dict(me) if me is not None else None),
            destinationId=node_num,
            portNum=port,
            wantResponse=True,
            channelIndex=channel_index,
            hopLimit=self._hop_limit(node_num),
        )
        row_id = self.db.log_exchange(
            kind=kind,
            node_num=node_num,
            direction="out",
            status="sent",
            channel=channel_index,
            packet_id=packet.id,
            origin=origin,
        )
        logger.info(
            "vnode: asked %s for %s on channel %d (id=%#010x, from %s)",
            proto.node_id(node_num),
            kind,
            channel_index,
            packet.id,
            origin,
        )
        row = self.db.exchange(row_id)
        stored = dict(row) if row is not None else {}
        self._emit_event("exchange", stored)
        return stored

    def _hop_limit(self, node_num: int) -> int | None:
        """Just far enough to reach the node, where we know how far that is.

        A traceroute at the node's full hop limit is repeated by everyone in
        range at every hop; asking a direct neighbour with 7 hops of budget is
        a lot of other people's airtime for one answer.
        """
        row = self.db.node(node_num)
        hops = row["hops_away"] if row is not None else None
        if hops is None:
            return None
        return max(1, min(int(hops) + 1, 7))

    # ---------------------------------------------------------------- sinks

    def _emit_state(self, state: str, detail: str) -> None:
        self.db.log_event(
            "info" if state in ("connected", "connecting") else "warn",
            "upstream",
            f"{state}: {detail}",
        )
        for sink in self._state_sinks:
            try:
                sink(state, detail)
            except Exception:
                logger.exception("vnode: state sink failed")

    def _on_config_captured(self, captured: list[tuple[str, str, int, bytes]]) -> None:
        if not captured:
            return
        self.db.replace_config_frames(captured)
        self._favorites = self.db.favorite_nums()
        # The handshake just refreshed every node's public key.
        if self.my_node_num:
            fixed = self.db.seal_own_dms(self.my_node_num, "webui")
            if fixed:
                logger.info("vnode: marked %d earlier web-UI DMs as PKI", fixed)
        self.config_captured_at = time.time()
        kinds: dict[str, int] = {}
        for _, kind, _, _ in captured:
            kinds[kind] = kinds.get(kind, 0) + 1
        logger.info("vnode: captured config handshake: %s", kinds)
        self.db.log_event("info", "upstream", f"config captured: {kinds}")

    def _on_raw_frame(self, raw: bytes, iface: _Interface) -> None:
        fr = proto.parse_from_radio(raw)
        which = fr.WhichOneof("payload_variant")

        if logger.isEnabledFor(TRACE):
            trace(
                logger,
                "<- node   %s%s | %s",
                proto.describe_from_radio(fr),
                " [config]" if iface._capturing else "",
                hexdump(raw),
            )

        if iface._capturing:
            keyed = proto.config_frame_key(fr)
            if keyed is None and which is None:
                # A variant newer than the meshtastic package's protobufs, such
                # as region_presets (firmware 2.8). Kept byte-for-byte in its
                # place in the sequence: the app knows what it is even if we don't.
                keyed = (f"unknown:{iface._config_ord}", "other")
            if keyed is not None:
                key, kind = keyed
                iface._captured.append((key, kind, iface._config_ord, raw))
                iface._config_ord += 1
            # NodeInfo arriving in the handshake also belongs in the node table.
            if which == "node_info":
                self._store_nodeinfo_frame(fr.node_info)
            return

        seq: int | None = None
        if which == "packet":
            seq = self._store_packet(fr.packet, raw)
        elif which == "queueStatus" and fr.queueStatus.mesh_packet_id:
            # The node took the packet into its transmit queue (res 0) or
            # refused it (anything else, e.g. queue full).
            q = fr.queueStatus
            if q.res == 0:
                self._set_status(q.mesh_packet_id, "sent", None, "queued by the node", "queued")
            else:
                self._set_status(
                    q.mesh_packet_id,
                    "failed",
                    f"queue refused ({q.res})",
                    "queue status",
                    "refused",
                    {"error": f"queue refused ({q.res})"},
                )

        ctx = FrameContext(during_config=False, seq=seq, kind=which)
        for sink in self._raw_sinks:
            try:
                sink(raw, ctx)
            except Exception:
                logger.exception("vnode: raw sink failed")

    # ---------------------------------------------------------------- storing

    @property
    def favorites(self) -> set[int]:
        return self._favorites

    def _tracked(self, node_num: int) -> bool:
        """Is this node's telemetry worth storing? Ours and the favourites only:
        on a mesh of hundreds of nodes the rest is noise."""
        return node_num == self.my_node_num or node_num in self._favorites

    def _store_packet(
        self, packet: mesh_pb2.MeshPacket, raw: bytes, origin: str | None = None
    ) -> int | None:
        meta = proto.packet_meta(packet)
        from_num = meta["from_num"]
        rx = meta["rx_time"] or int(time.time())
        portnum = meta["portnum"]

        # Every packet counts toward the traffic chart, stored or not.
        if origin is None:
            self.db.count_traffic(rx, portnum)
            # Keep the node list apps get on connect current: live position
            # and telemetry may not be forwarded to them at all.
            if from_num:
                self.db.refresh_node_frame(from_num, packet, int(time.time()))
                # Anything heard from a node proves it is alive, a text message
                # as much as a beacon. The port-specific handlers below add
                # their richer fields on top.
                self.db.upsert_node(from_num, {"last_heard": rx, "snr": meta["rx_snr"]})

        if portnum == proto.PORT_ROUTING:
            routing = proto.decode_routing(packet)
            if routing is not None and self.my_node_num:
                request_id, error = routing
                if self._routing_for_exchange(request_id, error):
                    # It answers a request, not a message: no delivery log.
                    return None
                status, detail = proto.delivery_status(from_num, self.my_node_num, error)
                if status == "failed":
                    event = "nak"
                elif status == "relayed":
                    event = "implicit_ack"
                else:
                    event = "ack"
                self._set_status(
                    request_id,
                    status,
                    detail,
                    f"routing from {proto.node_id(from_num)}",
                    event,
                    {
                        "ack_from": from_num,
                        "error": None if error == "NONE" else error,
                        **proto.ack_details(packet),
                    },
                )

        if portnum == proto.PORT_NODEINFO:
            info = proto.decode_nodeinfo(packet)
            if info:
                self.db.upsert_node(
                    from_num, {**info, "last_heard": meta["rx_time"], "snr": meta["rx_snr"]}
                )
        elif portnum == proto.PORT_POSITION:
            pos = proto.decode_position(packet)
            if pos is not None:
                fields = proto.position_fields(pos, meta["rx_time"])
                self.db.upsert_node(from_num, {**fields, "last_heard": meta["rx_time"]})
                if self._tracked(from_num):
                    self.db.store_position(from_num, fields)
                quality = proto.position_quality(pos)
                if quality and self._tracked(from_num):
                    self.db.store_telemetry(from_num, rx, "gps", quality)
        elif portnum == proto.PORT_TELEMETRY:
            tel = proto.decode_telemetry(packet)
            if tel:
                kind, values = tel
                if self._tracked(from_num):
                    self.db.store_telemetry(from_num, rx, kind, values)
                if kind == "device":
                    self.db.upsert_node(
                        from_num,
                        {
                            "battery_level": values.get("battery_level"),
                            "voltage": values.get("voltage"),
                            "last_heard": rx,
                        },
                    )

        if origin is None and from_num != self.my_node_num:
            self._note_exchange(packet, meta)

        if portnum not in proto.STORED_PORTNUMS:
            return None

        if origin is None:
            logger.info("vnode: text from the mesh: %s", proto.describe_packet(packet))

        meta["origin"] = origin
        seq = self.db.store_packet(raw=raw, meta=meta)
        if seq is None:
            # Expected and frequent: the node re-delivers what it hears twice.
            # Logged anyway, because "my message never showed up" and "it was
            # filed as a duplicate" look identical from the outside.
            logger.debug("vnode: duplicate, not stored: %s", proto.describe_packet(packet))
        else:
            logger.debug("vnode: stored seq=%d %s", seq, proto.describe_packet(packet))
        return seq

    # --------------------------------------------------------------- requests

    def _note_exchange(self, packet: mesh_pb2.MeshPacket, meta: dict) -> None:
        """File a packet that belongs to a request: either the answer to one we
        or a connected app sent, or a question another node asked of our node.

        Our node answers an incoming request by itself and does not report what
        it replied, so an inbound row records the question alone.
        """
        kind = proto.exchange_kind(meta["portnum"])
        if kind is None:
            return
        from_num = meta["from_num"]
        request_id = packet.decoded.request_id if packet.HasField("decoded") else 0
        pending = self.db.open_exchange(request_id)
        if pending is not None:
            row = self.db.resolve_exchange(
                pending["id"], status="answered", result=proto.exchange_result(kind, packet)
            )
            logger.info(
                "vnode: %s answered the %s request (id=%#010x)",
                proto.node_id(from_num),
                kind,
                request_id,
            )
            if row is not None:
                self._emit_event("exchange", dict(row))
            return

        if proto.is_request(packet) and meta["to_num"] == self.my_node_num:
            row_id = self.db.log_exchange(
                kind=kind,
                node_num=from_num,
                direction="in",
                status="heard",
                channel=meta["channel"],
                packet_id=meta["packet_id"],
                ts=meta["rx_time"] or int(time.time()),
            )
            logger.info("vnode: %s asked this node for %s", proto.node_id(from_num), kind)
            row = self.db.exchange(row_id)
            if row is not None:
                self._emit_event("exchange", dict(row))

    def _routing_for_exchange(self, request_id: int, error: str) -> bool:
        """Whether a routing packet answers an open request. A plain ack only
        says the question reached the mesh, so the request stays open until the
        answer itself arrives; an error ends it."""
        pending = self.db.open_exchange(request_id)
        if pending is None:
            return False
        if error != "NONE":
            row = self.db.resolve_exchange(pending["id"], status="failed", error=error)
            logger.info(
                "vnode: the %s request to %s failed: %s",
                pending["kind"],
                proto.node_id(pending["node_num"]),
                error,
            )
            if row is not None:
                self._emit_event("exchange", dict(row))
        return True

    # ------------------------------------------------------------- favourites

    def set_favorite(self, node_num: int, favorite: bool) -> None:
        """Star or unstar a node on the physical node itself, so the phone app
        and this service agree. Blocking - call from a thread."""
        iface = self.iface
        if iface is None:
            raise RuntimeError("upstream not connected")
        if favorite:
            iface.localNode.setFavorite(node_num)
        else:
            iface.localNode.removeFavorite(node_num)
        self.note_node_flags(node_num, is_favorite=favorite)
        logger.info(
            "vnode: %s %s on the node",
            "favourited" if favorite else "unfavourited",
            proto.node_id(node_num),
        )

    def note_node_flags(
        self, node_num: int, *, is_favorite: bool | None = None, is_ignored: bool | None = None
    ) -> None:
        """Record a flag change the node has been told about."""
        self.db.set_node_flags(node_num, is_favorite=is_favorite, is_ignored=is_ignored)
        self._favorites = self.db.favorite_nums()

    def store_client_packet(self, packet: mesh_pb2.MeshPacket, origin: str) -> int | None:
        """Store a packet a downstream client sent, so the web UI and the other
        clients see it. The node does not echo our own transmissions back."""
        my_num = self.my_node_num or 0
        if not packet.__getattribute__("from") and my_num:
            packet = mesh_pb2.MeshPacket.FromString(packet.SerializeToString())
            packet.__setattr__("from", my_num)
        if not packet.rx_time:
            packet.rx_time = int(time.time())
        raw = proto.wrap_packet(packet)
        seq = self._store_packet(packet, raw, origin=origin)
        if seq is not None:
            self._apply_early_status(packet.id)
        return seq

    def _store_nodeinfo_frame(self, info) -> None:
        fields = {
            "node_id": info.user.id or proto.node_id(info.num),
            "long_name": info.user.long_name or None,
            "short_name": info.user.short_name or None,
            "hw_model": (
                mesh_pb2.HardwareModel.Name(info.user.hw_model) if info.user.hw_model else None
            ),
            "role": (
                config_pb2.Config.DeviceConfig.Role.Name(info.user.role) if info.user.role else None
            ),
            "last_heard": info.last_heard or None,
            "snr": info.snr or None,
            "hops_away": info.hops_away if info.HasField("hops_away") else None,
            "battery_level": info.device_metrics.battery_level or None,
            "voltage": round(info.device_metrics.voltage, 3)
            if info.device_metrics.voltage
            else None,
            "is_favorite": int(info.is_favorite),
            "is_ignored": int(info.is_ignored),
            "via_mqtt": int(info.via_mqtt),
            "public_key": bytes(info.user.public_key) or None,
        }
        if info.position.latitude_i or info.position.longitude_i:
            fields["latitude"] = info.position.latitude_i * 1e-7
            fields["longitude"] = info.position.longitude_i * 1e-7
            fields["precision_bits"] = info.position.precision_bits or None
            fields["position_time"] = info.position.time or None
        self.db.upsert_node(info.num, fields)
