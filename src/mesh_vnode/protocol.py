"""Protobuf helpers, built on the protobufs shipped with the `meshtastic` package.

Nothing here re-implements the wire format; it only decodes enough of a FromRadio
to file it in SQLite and builds the handful of frames a virtual node has to
synthesise itself (config_complete, queue status).
"""

from __future__ import annotations

import contextlib
from typing import Any

from meshtastic.protobuf import admin_pb2, config_pb2, mesh_pb2, portnums_pb2, telemetry_pb2

BROADCAST_NUM = 0xFFFFFFFF

PORT_TEXT = portnums_pb2.PortNum.TEXT_MESSAGE_APP  # 1
PORT_POSITION = portnums_pb2.PortNum.POSITION_APP  # 3
PORT_NODEINFO = portnums_pb2.PortNum.NODEINFO_APP  # 4
PORT_ROUTING = portnums_pb2.PortNum.ROUTING_APP  # 5
PORT_ADMIN = portnums_pb2.PortNum.ADMIN_APP  # 6
PORT_TELEMETRY = portnums_pb2.PortNum.TELEMETRY_APP  # 67
PORT_TRACEROUTE = portnums_pb2.PortNum.TRACEROUTE_APP  # 70

# The ports that carry a question rather than a statement: sent with
# want_response set, the receiving node answers on the same port. Everything
# here can be asked of one node from the web UI, and recognised when another
# node asks it of ours.
EXCHANGE_PORTS = {
    "traceroute": PORT_TRACEROUTE,
    "position": PORT_POSITION,
    "telemetry": PORT_TELEMETRY,
    "nodeinfo": PORT_NODEINFO,
}
EXCHANGE_KINDS = tuple(EXCHANGE_PORTS)

# Ports whose raw packets are kept. Only text: it is what gets replayed, and on
# a busy mesh everything else is noise. A real node here heard ~75 position and
# telemetry packets a minute from 250 nodes - about 100k rows a day. Position,
# node info and telemetry still update the `nodes` and `telemetry` tables (one
# row per node, and telemetry only for this node and its favourites); every
# packet is counted per hour in `traffic`.
STORED_PORTNUMS = (PORT_TEXT,)
REPLAY_PORTNUMS = (PORT_TEXT,)

# Ports a downstream client is not allowed to push upstream unless
# VNODE_ALLOW_ADMIN is set. Admin packets reconfigure the physical node, which an
# app reaching it through a proxy has no business doing.
#
# MeshMonitor blocks 6 and 8 and labels 8 "NODEINFO_APP" in a comment, but 8 is
# WAYPOINT_APP; NODEINFO_APP is 4. Their block list therefore stops waypoints and
# leaves node info alone. Node info is how a node's name reaches the rest of the
# mesh and waypoints are ordinary user content, so neither is blocked here.
# MeshMonitor: https://github.com/Yeraze/meshmonitor
BLOCKED_PORTNUMS = (PORT_ADMIN,)

# want_config_id nonces with special meaning in the firmware's PhoneAPI:
# https://github.com/meshtastic/firmware/blob/3468af94aa0f79e93c9bf041244bf230161fc704/src/mesh/PhoneAPI.cpp
NONCE_ONLY_CONFIG = 69420  # skip the other-node NodeInfos
NONCE_ONLY_DB = 69421  # only MyNodeInfo + NodeInfos, no channels/config


def node_id(num: int) -> str:
    return f"!{num & 0xFFFFFFFF:08x}"


def parse_from_radio(raw: bytes) -> mesh_pb2.FromRadio:
    msg = mesh_pb2.FromRadio()
    msg.ParseFromString(raw)
    return msg


def parse_to_radio(raw: bytes) -> mesh_pb2.ToRadio:
    msg = mesh_pb2.ToRadio()
    msg.ParseFromString(raw)
    return msg


def config_frame_key(fr: mesh_pb2.FromRadio) -> tuple[str, str] | None:
    """Map a config-phase FromRadio to a (key, kind) pair.

    The key is stable across reconnects so a refreshed handshake overwrites the
    previous capture instead of piling up.
    """
    which = fr.WhichOneof("payload_variant")
    if which == "my_info":
        return "my_info", "my_info"
    if which == "metadata":
        return "metadata", "metadata"
    if which == "channel":
        return f"channel:{fr.channel.index}", "channel"
    if which == "config":
        return f"config:{fr.config.WhichOneof('payload_variant')}", "config"
    if which == "moduleConfig":
        return f"moduleConfig:{fr.moduleConfig.WhichOneof('payload_variant')}", "moduleConfig"
    if which == "node_info":
        return f"node_info:{fr.node_info.num}", "node_info"
    if which == "deviceuiConfig":
        return "deviceuiConfig", "other"
    if which == "fileInfo":
        return f"fileInfo:{fr.fileInfo.file_name}", "other"
    return None


def packet_meta(packet: mesh_pb2.MeshPacket) -> dict[str, Any]:
    """Flatten a MeshPacket into the columns `packets` stores."""
    meta: dict[str, Any] = {
        "packet_id": packet.id,
        "from_num": packet.__getattribute__("from"),
        "to_num": packet.to,
        "channel": packet.channel,
        "rx_time": packet.rx_time or None,
        "want_ack": packet.want_ack,
        "hop_limit": packet.hop_limit,
        "hop_start": packet.hop_start,
        "rx_snr": packet.rx_snr or None,
        "rx_rssi": packet.rx_rssi or None,
        "pki": packet.pki_encrypted,
        "portnum": 0,
        "text": None,
        "reply_id": 0,
        "emoji": 0,
    }
    if packet.HasField("decoded"):
        meta["portnum"] = int(packet.decoded.portnum)
        # A reaction is a text packet with `emoji` set whose payload is the
        # emoji itself; `reply_id` names the message it reacts to.
        meta["reply_id"] = packet.decoded.reply_id
        meta["emoji"] = packet.decoded.emoji
        if meta["portnum"] == PORT_TEXT:
            try:
                meta["text"] = packet.decoded.payload.decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover - decode already tolerant
                meta["text"] = None
    return meta


def decode_nodeinfo(packet: mesh_pb2.MeshPacket) -> dict[str, Any] | None:
    if not packet.HasField("decoded") or packet.decoded.portnum != PORT_NODEINFO:
        return None
    user = mesh_pb2.User()
    try:
        user.ParseFromString(packet.decoded.payload)
    except Exception:
        return None
    return {
        "node_id": user.id or node_id(packet.__getattribute__("from")),
        "long_name": user.long_name or None,
        "short_name": user.short_name or None,
        "hw_model": mesh_pb2.HardwareModel.Name(user.hw_model) if user.hw_model else None,
        "role": config_pb2.Config.DeviceConfig.Role.Name(user.role) if user.role else None,
        "is_licensed": int(user.is_licensed),
        "public_key": bytes(user.public_key) or None,
    }


def decode_position(packet: mesh_pb2.MeshPacket) -> mesh_pb2.Position | None:
    if not packet.HasField("decoded") or packet.decoded.portnum != PORT_POSITION:
        return None
    pos = mesh_pb2.Position()
    try:
        pos.ParseFromString(packet.decoded.payload)
    except Exception:
        return None
    return pos


def position_fields(pos: mesh_pb2.Position, heard_at: int | None = None) -> dict[str, Any]:
    """Coordinates, for the `nodes` table.

    `precision_bits` travels with them: a node sharing a blurred position sends
    the centre of a grid cell, and the map draws that cell's size as a circle
    rather than pretending the dot is exact.
    """
    out: dict[str, Any] = {}
    if pos.latitude_i:
        out["latitude"] = pos.latitude_i * 1e-7
    if pos.longitude_i:
        out["longitude"] = pos.longitude_i * 1e-7
    if pos.HasField("altitude"):
        out["altitude"] = pos.altitude
    if "latitude" in out or "longitude" in out:
        out["precision_bits"] = pos.precision_bits or None
        out["position_time"] = pos.time or heard_at or None
    return out


def position_quality(pos: mesh_pb2.Position) -> dict[str, Any] | None:
    """GPS fix quality, for the telemetry page.

    DOP values arrive in 1/100 units (141 means 1.41). Lower is better: under 2
    is a good fix, over 5 a poor one. Many receivers report only PDOP.
    `precision_bits` is different: not how good the fix is, but how much of it
    the node shares on the channel (32 = exact, lower = deliberately blurred).
    """
    out: dict[str, Any] = {
        "sats_in_view": pos.sats_in_view or None,
        "pdop": pos.PDOP / 100 if pos.PDOP else None,
        "hdop": pos.HDOP / 100 if pos.HDOP else None,
        "vdop": pos.VDOP / 100 if pos.VDOP else None,
        "gps_accuracy_mm": pos.gps_accuracy or None,
        "precision_bits": pos.precision_bits or None,
        "fix_type": pos.fix_type or None,
        "fix_quality": pos.fix_quality or None,
    }
    if all(v is None for v in out.values()):
        return None
    return out


# How long since a node was last heard still counts as online. The firmware's
# own num_online_nodes uses the same two hours (NUM_ONLINE_SECS), so counting
# ours the same way makes the two comparable.
ONLINE_WINDOW_S = 2 * 3600


def decode_telemetry(packet: mesh_pb2.MeshPacket) -> tuple[str, dict[str, Any]] | None:
    """Return (kind, values) for a telemetry packet."""
    if not packet.HasField("decoded") or packet.decoded.portnum != PORT_TELEMETRY:
        return None
    tel = telemetry_pb2.Telemetry()
    try:
        tel.ParseFromString(packet.decoded.payload)
    except Exception:
        return None
    variant = tel.WhichOneof("variant")
    if variant is None:
        return None
    # A node asking another for telemetry names the variant it wants and leaves
    # it empty (want_response carries the question). That is not a reading:
    # stored as one it would chart as a node with no uptime, no counters and no
    # nodes known, and the packet-rate derivation would then read the next real
    # sample as a jump from zero.
    if not getattr(tel, variant).ByteSize():
        return None
    if variant == "device_metrics":
        m = tel.device_metrics
        return "device", {
            "battery_level": m.battery_level or None,
            "voltage": round(m.voltage, 3) if m.voltage else None,
            "channel_utilization": round(m.channel_utilization, 3) or None,
            "air_util_tx": round(m.air_util_tx, 3) or None,
            "uptime_seconds": m.uptime_seconds or None,
        }
    if variant == "environment_metrics":
        m = tel.environment_metrics
        return "environment", {
            "temperature": round(m.temperature, 2) if m.HasField("temperature") else None,
            "relative_humidity": (
                round(m.relative_humidity, 2) if m.HasField("relative_humidity") else None
            ),
            "barometric_pressure": (
                round(m.barometric_pressure, 2) if m.HasField("barometric_pressure") else None
            ),
        }
    if variant == "local_stats":
        # Only ever sent by the node we are connected to, about itself.
        m = tel.local_stats
        return "local", {
            "channel_utilization": round(m.channel_utilization, 3) or None,
            "air_util_tx": round(m.air_util_tx, 3) or None,
            "uptime_seconds": m.uptime_seconds or None,
            "noise_floor": m.noise_floor or None,
            "num_packets_tx": m.num_packets_tx,
            "num_packets_rx": m.num_packets_rx,
            "num_packets_rx_bad": m.num_packets_rx_bad,
            "num_rx_dupe": m.num_rx_dupe,
            "num_tx_relay": m.num_tx_relay,
            "num_tx_relay_canceled": m.num_tx_relay_canceled,
            "num_tx_dropped": m.num_tx_dropped,
            "num_online_nodes": m.num_online_nodes,
            "num_total_nodes": m.num_total_nodes,
        }
    if variant == "power_metrics":
        m = tel.power_metrics
        return "power", {
            "ch1_voltage": round(m.ch1_voltage, 3) if m.HasField("ch1_voltage") else None,
            "ch1_current": round(m.ch1_current, 3) if m.HasField("ch1_current") else None,
        }
    return None


# --------------------------------------------------------------- synthesised


def build_config_complete(config_id: int) -> bytes:
    fr = mesh_pb2.FromRadio()
    fr.config_complete_id = config_id
    return fr.SerializeToString()


def build_queue_status(free: int = 16, maxlen: int = 16, res: int = 0, packet_id: int = 0) -> bytes:
    fr = mesh_pb2.FromRadio()
    fr.queueStatus.res = res
    fr.queueStatus.free = free
    fr.queueStatus.maxlen = maxlen
    fr.queueStatus.mesh_packet_id = packet_id
    return fr.SerializeToString()


def wrap_packet(packet: mesh_pb2.MeshPacket, *, override_from: int | None = None) -> bytes:
    """Wrap a MeshPacket in a FromRadio, as the node would when echoing it back."""
    fr = mesh_pb2.FromRadio()
    fr.packet.CopyFrom(packet)
    if override_from is not None and not packet.__getattribute__("from"):
        fr.packet.__setattr__("from", override_from)
    return fr.SerializeToString()


def fill_from_if_zero(to_radio_bytes: bytes, node_num: int) -> bytes:
    """Stamp our node number into a packet the client left at from=0.

    An app hands its node a packet with the sender field zeroed, meaning "you are
    the node, fill this in", and the node stamps itself before transmitting.
    Through a proxy that never happens, and a PKI-encrypted DM built this way is
    rejected. Filling the field in is what the node would have done anyway, and
    unlike stripping the encryption it costs nothing: the payload is untouched
    and the DM stays sealed to its recipient.

    Returns the original bytes unchanged when there is nothing to do, so callers
    can test identity to see whether it fired.
    """
    if not node_num:
        return to_radio_bytes
    try:
        tr = parse_to_radio(to_radio_bytes)
    except Exception:
        return to_radio_bytes
    if tr.WhichOneof("payload_variant") != "packet":
        return to_radio_bytes
    if tr.packet.__getattribute__("from") != 0:
        return to_radio_bytes
    tr.packet.__setattr__("from", node_num)
    return tr.SerializeToString()


# -------------------------------------------------------------------- routing


def decode_routing(packet: mesh_pb2.MeshPacket) -> tuple[int, str] | None:
    """(request_id, error name) for an ack or nak, None for anything else.

    The node reports the fate of a packet we sent with a ROUTING_APP packet
    whose `request_id` is that packet's id. `error_reason` NONE is an ack,
    anything else a failure (NO_RESPONSE, MAX_RETRANSMIT, PKI_FAILED, ...).
    """
    if not packet.HasField("decoded") or packet.decoded.portnum != PORT_ROUTING:
        return None
    request_id = packet.decoded.request_id
    if not request_id:
        return None
    routing = mesh_pb2.Routing()
    try:
        routing.ParseFromString(packet.decoded.payload)
    except Exception:
        return None
    return request_id, mesh_pb2.Routing.Error.Name(routing.error_reason)


def heard_fields(packet: mesh_pb2.MeshPacket) -> dict[str, Any]:
    """What a received packet says about how far away its sender is.

    `hop_start` is the hop limit the sender set out with and `hop_limit` what is
    left, so the difference is how many hops the packet took. Two cases say
    nothing about distance and leave the stored value alone: firmware older than
    2.2 leaves `hop_start` at 0, and a packet that reached us over MQTT was
    injected with its hop counters untouched, which would read as a direct
    neighbour however far away the node really is.
    """
    fields: dict[str, Any] = {"via_mqtt": int(packet.via_mqtt)}
    if packet.hop_start and packet.hop_limit <= packet.hop_start and not packet.via_mqtt:
        fields["hops_away"] = packet.hop_start - packet.hop_limit
    return fields


def ack_details(packet: mesh_pb2.MeshPacket) -> dict[str, Any]:
    """What an ack packet tells about how it got here, for the delivery log.

    For the node's own implicit ack the firmware copies the overheard
    rebroadcast's `relay_node` and the SNR/RSSI it was heard at into the ack
    (MeshModule::allocAckNak), so this names who repeated our message. For a
    recipient's ack it is the ack's own path back to us. `relay_node` is only
    the last byte of a node number, so it can match more than one node.
    """
    hops = packet.hop_start - packet.hop_limit if packet.hop_start else None
    return {
        "relay_node": packet.relay_node or None,
        "rx_snr": round(packet.rx_snr, 2) or None,
        "rx_rssi": packet.rx_rssi or None,
        "hops": hops,
    }


def delivery_status(ack_from: int, my_num: int, error: str) -> tuple[str, str | None]:
    """What an ack or nak means for the message it answers.

    The firmware's own ack (from our node number) is the implicit one: it heard
    another node repeat the message, so it made it onto the mesh. An ack from
    any other node comes from the recipient itself, which is the confirmation.
    Channel messages have no single recipient, so the implicit ack is the most
    they ever get.
    """
    if error != "NONE":
        return "failed", error
    # from=0 is how a locally generated packet can look before the node stamps
    # it; reading that as "the recipient confirmed" would be a false double tick.
    if ack_from in (0, my_num):
        return "relayed", None
    return "delivered", None


# ---------------------------------------------------------------- exchanges

# An unknown SNR in a RouteDiscovery, in the same 1/4 dB units as the rest.
SNR_UNKNOWN = -128


def exchange_kind(portnum: int) -> str | None:
    """Which exchange a port belongs to, or None for anything else."""
    for kind, port in EXCHANGE_PORTS.items():
        if portnum == port:
            return kind
    return None


def request_payload(kind: str, *, me: dict[str, Any] | None = None) -> bytes:
    """The payload that asks a node for something.

    Position and node info are exchanges rather than plain questions: the phone
    apps send their own position or user with want_response set, and the other
    node answers with its own. `me` is our node's row, so we send what it knows
    about itself; an empty payload still works if we know nothing yet.
    """
    if kind == "traceroute":
        return mesh_pb2.RouteDiscovery().SerializeToString()
    if kind == "telemetry":
        # No local metrics attached: the question is what the other node has.
        return telemetry_pb2.Telemetry().SerializeToString()
    if kind == "position":
        pos = mesh_pb2.Position()
        if me and me.get("latitude") is not None and me.get("longitude") is not None:
            pos.latitude_i = int(me["latitude"] / 1e-7)
            pos.longitude_i = int(me["longitude"] / 1e-7)
            if me.get("altitude") is not None:
                pos.altitude = int(me["altitude"])
            if me.get("precision_bits"):
                pos.precision_bits = int(me["precision_bits"])
        return pos.SerializeToString()
    if kind == "nodeinfo":
        user = mesh_pb2.User()
        if me:
            user.id = me.get("node_id") or node_id(me.get("node_num", 0))
            user.long_name = me.get("long_name") or ""
            user.short_name = me.get("short_name") or ""
            if me.get("hw_model"):
                # The name came from this enum in the first place, but a node
                # stored by an older protobuf may name a model we cannot map.
                with contextlib.suppress(ValueError):
                    user.hw_model = mesh_pb2.HardwareModel.Value(me["hw_model"])
            if me.get("public_key"):
                user.public_key = bytes(me["public_key"])
        return user.SerializeToString()
    raise ValueError(f"unknown exchange kind: {kind}")


def _snr_list(values: Any) -> list[float | None]:
    """RouteDiscovery SNRs travel in 1/4 dB, with -128 meaning "not known"."""
    return [None if v == SNR_UNKNOWN else v / 4 for v in values]


def decode_route_discovery(packet: mesh_pb2.MeshPacket) -> dict[str, Any] | None:
    """The route a traceroute answer carries.

    `route` is the path the request took towards the destination and
    `route_back` the path the answer took home; they usually mirror each other
    but need not. Each SNR list has one more entry than its route: the last one
    is the final hop into the node that reports it.
    """
    if not packet.HasField("decoded") or packet.decoded.portnum != PORT_TRACEROUTE:
        return None
    rd = mesh_pb2.RouteDiscovery()
    try:
        rd.ParseFromString(packet.decoded.payload)
    except Exception:
        return None
    return {
        "route": list(rd.route),
        "snr_towards": _snr_list(rd.snr_towards),
        "route_back": list(rd.route_back),
        "snr_back": _snr_list(rd.snr_back),
        "hops": packet.hop_start - packet.hop_limit if packet.hop_start else None,
    }


def is_request(packet: mesh_pb2.MeshPacket) -> bool:
    """Does this packet ask for an answer? Set on the question, never on the
    answer, so it tells the two apart on the same port."""
    return packet.HasField("decoded") and bool(packet.decoded.want_response)


def exchange_result(kind: str, packet: mesh_pb2.MeshPacket) -> dict[str, Any] | None:
    """What an answer says, decoded for storage. The node tables are updated
    from the same packet elsewhere; this is the copy kept with the request."""
    if kind == "traceroute":
        return decode_route_discovery(packet)
    if kind == "position":
        pos = decode_position(packet)
        return position_fields(pos) if pos is not None else None
    if kind == "telemetry":
        tel = decode_telemetry(packet)
        if tel is None:
            return None
        metric, values = tel
        return {"metric": metric, **{k: v for k, v in values.items() if v is not None}}
    if kind == "nodeinfo":
        info = decode_nodeinfo(packet)
        if info is None:
            return None
        # The key is bytes and of no use in a JSON blob the web UI reads.
        return {k: v for k, v in info.items() if k != "public_key" and v is not None}
    return None


# ------------------------------------------------------------ app forwarding

# Live traffic an app can be spared, by category. Text, and anything to or
# from our own node, always goes through (see `forward_category`).
FORWARD_CATEGORIES = ("position", "telemetry", "nodeinfo", "other")
ALWAYS_FORWARDED_PORTS = (PORT_TEXT, portnums_pb2.PortNum.TEXT_MESSAGE_COMPRESSED_APP)


def forward_category(fr: mesh_pb2.FromRadio, my_num: int | None) -> str | None:
    """Which switchable category a live frame falls in, or None if it must
    always reach the app.

    Always: every frame that is not a mesh packet (queue status, reboot
    notices, ...), text and reactions, and packets to or from our own node -
    acks for the app's messages and answers to what it asked for (config,
    traceroute, position requests) among them.
    """
    if fr.WhichOneof("payload_variant") != "packet":
        return None
    p = fr.packet
    if my_num and my_num in (p.__getattribute__("from"), p.to):
        return None
    if not p.HasField("decoded"):
        return "other"  # a channel this node cannot read
    port = p.decoded.portnum
    if port in ALWAYS_FORWARDED_PORTS:
        return None
    return {PORT_POSITION: "position", PORT_TELEMETRY: "telemetry", PORT_NODEINFO: "nodeinfo"}.get(
        port, "other"
    )


def refreshed_node_info(
    raw: bytes | None, packet: mesh_pb2.MeshPacket, *, now: int
) -> bytes | None:
    """A handshake node_info frame brought up to date by a live packet.

    Apps get the node list from the stored handshake, which is only
    re-captured when the link to the node reconnects. Kept current here -
    names, keys, position, battery, last heard - every app connect brings the
    app up to date, even when live position and telemetry are not forwarded.

    `raw` is the stored frame, or None for a node not in it yet. A new node is
    only added once its node info (name, key) has been heard, the way the
    firmware's own list fills. Returns None when there is nothing to store.
    """
    frm = packet.__getattribute__("from")
    fr = parse_from_radio(raw) if raw else None
    port = packet.decoded.portnum if packet.HasField("decoded") else None
    user = None
    if port == PORT_NODEINFO:
        user = mesh_pb2.User()
        try:
            user.ParseFromString(packet.decoded.payload)
        except Exception:
            user = None
    if fr is None:
        if user is None:
            return None
        fr = mesh_pb2.FromRadio()
        fr.node_info.num = frm
    info = fr.node_info

    if user is not None:
        # Keep a key we already had if this announcement leaves it out.
        known_key = bytes(info.user.public_key)
        info.user.CopyFrom(user)
        if not info.user.public_key and known_key:
            info.user.public_key = known_key
    elif port == PORT_POSITION:
        pos = mesh_pb2.Position()
        try:
            pos.ParseFromString(packet.decoded.payload)
        except Exception:
            pos = None
        if pos is not None and (pos.latitude_i or pos.longitude_i):
            info.position.CopyFrom(pos)
            if not info.position.time:
                info.position.time = packet.rx_time or now
    elif port == PORT_TELEMETRY:
        tel = telemetry_pb2.Telemetry()
        try:
            tel.ParseFromString(packet.decoded.payload)
        except Exception:
            tel = None
        if tel is not None and tel.WhichOneof("variant") == "device_metrics":
            info.device_metrics.CopyFrom(tel.device_metrics)

    info.last_heard = packet.rx_time or now
    if packet.rx_snr:
        info.snr = packet.rx_snr
    heard = heard_fields(packet)
    if "hops_away" in heard:
        info.hops_away = heard["hops_away"]
    info.via_mqtt = bool(heard["via_mqtt"])
    if packet.HasField("decoded"):
        info.channel = packet.channel
    return fr.SerializeToString()


# ---------------------------------------------------------------------- admin

# Admin messages a phone may send through the proxy even with admin blocked.
# They only change how the node's own database flags another node, the same
# thing the app does when you tap the star. MeshMonitor allows the favourite
# pair for the same reason.
SAFE_ADMIN_VARIANTS = (
    "set_favorite_node",
    "remove_favorite_node",
    "set_ignored_node",
    "remove_ignored_node",
)


# Admin requests that only read. Their answers - config, owner, channels,
# metadata - are what the handshake hands every app anyway, so letting an app
# ask again exposes nothing new. Everything that writes stays blocked.
READ_ONLY_ADMIN_VARIANTS = (
    "get_channel_request",
    "get_owner_request",
    "get_config_request",
    "get_module_config_request",
    "get_canned_message_module_messages_request",
    "get_device_metadata_request",
    "get_ringtone_request",
    "get_device_connection_status_request",
    "get_node_remote_hardware_pins_request",
    "get_ui_config_request",
)


# Writes the phone app sends on its own, every time it connects, that we block
# by design and that nothing depends on. Still dropped - just not worth a
# warning per connect, which buried the blocks that do deserve a look.
ROUTINE_BLOCKED_ADMIN_VARIANTS = ("set_time_only",)


def describe_admin(packet: mesh_pb2.MeshPacket) -> tuple[str | None, str]:
    """(variant, human description) of an admin packet, for filtering and logs.

    The variant is None when the payload does not parse or uses a field newer
    than the meshtastic package knows; such a message is never treated as
    read-only.
    """
    msg = parse_admin(packet)
    if msg is None:
        return None, "undecodable admin message"
    variant = msg.WhichOneof("payload_variant")
    if variant is None:
        return (
            None,
            f"admin message the meshtastic package does not know ({len(packet.decoded.payload)}B)",
        )
    value = getattr(msg, variant)
    if variant == "get_config_request":
        value = admin_pb2.AdminMessage.ConfigType.Name(value)
    elif variant == "get_module_config_request":
        value = admin_pb2.AdminMessage.ModuleConfigType.Name(value)
    elif isinstance(value, (bool, int)):
        value = value if not isinstance(value, bool) else ""
    else:
        value = type(value).__name__
    return variant, f"{variant}{f'={value}' if value != '' else ''}"


def parse_admin(packet: mesh_pb2.MeshPacket) -> admin_pb2.AdminMessage | None:
    if not packet.HasField("decoded") or packet.decoded.portnum != PORT_ADMIN:
        return None
    msg = admin_pb2.AdminMessage()
    try:
        msg.ParseFromString(packet.decoded.payload)
    except Exception:
        return None
    return msg


def safe_admin_change(packet: mesh_pb2.MeshPacket) -> tuple[str, int] | None:
    """If this is one of the harmless admin messages, return (variant, node_num)."""
    msg = parse_admin(packet)
    if msg is None:
        return None
    variant = msg.WhichOneof("payload_variant")
    if variant in SAFE_ADMIN_VARIANTS:
        return variant, getattr(msg, variant)
    return None


def with_node_flags(
    raw: bytes, *, is_favorite: bool | None = None, is_ignored: bool | None = None
) -> bytes:
    """A captured node_info frame with its favourite/ignored flags changed.

    The handshake is replayed from storage, so without this a phone would keep
    seeing the old star until the node reconnected. Only these two flags are
    touched; everything else in the frame stays as the node sent it.
    """
    fr = parse_from_radio(raw)
    if fr.WhichOneof("payload_variant") != "node_info":
        return raw
    if is_favorite is not None:
        fr.node_info.is_favorite = is_favorite
    if is_ignored is not None:
        fr.node_info.is_ignored = is_ignored
    return fr.SerializeToString()


# Display names of the LoRa presets, keyed by enum number rather than name: the
# meshtastic package's protobufs predate several of them. Copied from the
# firmware, including its "LongMod" abbreviation:
# https://github.com/meshtastic/firmware/blob/3468af94aa0f79e93c9bf041244bf230161fc704/src/DisplayFormatters.cpp
MODEM_PRESET_NAMES = {
    0: "LongFast",
    1: "LongSlow",
    3: "MediumSlow",
    4: "MediumFast",
    5: "ShortSlow",
    6: "ShortFast",
    7: "LongMod",
    8: "ShortTurbo",
    9: "LongTurbo",
    10: "LiteFast",
    11: "LiteSlow",
    12: "NarrowFast",
    13: "NarrowSlow",
    14: "TinyFast",
    15: "TinySlow",
    16: "MediumTurbo",
}


def channel_name(name: str, lora: config_pb2.Config.LoRaConfig | None) -> str:
    """A channel's name as the node and the app show it.

    A channel left unnamed is called after the modem preset ("LongFast"), or
    "Custom" when the radio is not on a preset - the same rule as the
    firmware's Channels::getName.
    """
    if name:
        return name
    if lora is None:
        return "LongFast"  # the firmware default, before a lora config is known
    if not lora.use_preset:
        return "Custom"
    return MODEM_PRESET_NAMES.get(lora.modem_preset, "Invalid")


# The firmware never shares a position more exactly than this on a channel
# whose key is public (PositionPrecision.h).
MAX_POSITION_PRECISION_PUBLIC_KEY = 15


def channel_info(channels: list, lora) -> list[dict[str, Any]]:
    """What each enabled channel shares and how it is protected, from the
    captured Channel frames - the same rules the firmware applies."""
    by_index = {c.index: c for c in channels}
    primary = next((c for c in channels if c.role == 1), None)

    def key_kind(ch) -> str:
        psk = bytes(ch.settings.psk)
        if not psk and ch.role == 2 and primary is not None:  # secondary inherits
            return key_kind(primary)
        if not psk:
            return "none"
        if len(psk) == 1:
            return "none" if psk[0] == 0 else "default"
        return f"aes{len(psk) * 8}"

    out = []
    position_channel = None
    for index in sorted(by_index):
        ch = by_index[index]
        if ch.role == 0:  # DISABLED
            continue
        key = key_kind(ch)
        public = key in ("none", "default")
        precision = ch.settings.module_settings.position_precision
        if public and precision > MAX_POSITION_PRECISION_PUBLIC_KEY:
            precision = MAX_POSITION_PRECISION_PUBLIC_KEY
        if precision and position_channel is None:
            position_channel = index
        out.append(
            {
                "index": index,
                "role": "PRIMARY" if ch.role == 1 else "SECONDARY",
                "name": channel_name(ch.settings.name, lora),
                "key": key,
                "public": public,
                "position_precision": precision,
                "position_meters": precision_bits_meters(precision),
                "muted_on_node": ch.settings.module_settings.is_muted,
                "uplink": ch.settings.uplink_enabled,
                "downlink": ch.settings.downlink_enabled,
            }
        )
    # The node broadcasts its position on the first channel that shares one.
    for c in out:
        c["carries_position"] = c["index"] == position_channel
    return out


def build_routing_error(my_num: int, request_id: int, error: int) -> bytes:
    """A nak from "the node" for a packet we refused to forward, so the app
    reports a failure instead of waiting for an answer that never comes."""
    fr = mesh_pb2.FromRadio()
    p = fr.packet
    p.__setattr__("from", my_num)
    p.to = my_num
    p.decoded.portnum = PORT_ROUTING
    p.decoded.request_id = request_id
    p.decoded.payload = mesh_pb2.Routing(error_reason=error).SerializeToString()
    return fr.SerializeToString()


def precision_bits_meters(bits: int) -> float | None:
    """Worst-case error of a position shared with this many precision bits."""
    if not bits:
        return None
    if bits >= 32:
        return 0.0
    # Each coordinate is truncated to a grid of 2^(32-bits) units of 1e-7
    # degrees; the error is up to half a cell. ~111 km per degree.
    return (2 ** (32 - bits)) * 1e-7 * 111_320 / 2


# ------------------------------------------------------------------ describing


def port_name(portnum: int) -> str:
    try:
        return portnums_pb2.PortNum.Name(portnum)
    except ValueError:
        return f"PORT_{portnum}"


def describe_packet(packet: mesh_pb2.MeshPacket) -> str:
    """One line for a MeshPacket, for logs. Text payloads are truncated, not
    dropped: the message content is usually what identifies the packet."""
    frm = packet.__getattribute__("from")
    to = "^all" if packet.to == BROADCAST_NUM else node_id(packet.to)
    parts = [
        f"id={packet.id:#010x}",
        f"{node_id(frm)}->{to}",
        f"ch={packet.channel}",
    ]
    if packet.HasField("decoded"):
        portnum = int(packet.decoded.portnum)
        parts.append(port_name(portnum))
        if portnum == PORT_TEXT:
            text = packet.decoded.payload.decode("utf-8", "replace")
            parts.append(repr(text[:40] + ("…" if len(text) > 40 else "")))
        else:
            parts.append(f"{len(packet.decoded.payload)}B")
    elif packet.encrypted:
        parts.append(f"ENCRYPTED {len(packet.encrypted)}B")
    if packet.pki_encrypted:
        parts.append("pki")
    if packet.want_ack:
        parts.append("want_ack")
    if packet.hop_start:
        parts.append(f"hops={packet.hop_start - packet.hop_limit}/{packet.hop_start}")
    return " ".join(parts)


def describe_from_radio(fr: mesh_pb2.FromRadio) -> str:
    which = fr.WhichOneof("payload_variant")
    if which == "packet":
        return f"packet {describe_packet(fr.packet)}"
    if which == "config_complete_id":
        return f"config_complete_id={fr.config_complete_id}"
    if which == "my_info":
        return f"my_info node={node_id(fr.my_info.my_node_num)}"
    if which == "metadata":
        return f"metadata fw={fr.metadata.firmware_version}"
    if which == "channel":
        return f"channel index={fr.channel.index} name={fr.channel.settings.name!r}"
    if which == "config":
        return f"config {fr.config.WhichOneof('payload_variant')}"
    if which == "moduleConfig":
        return f"moduleConfig {fr.moduleConfig.WhichOneof('payload_variant')}"
    if which == "node_info":
        return f"node_info {node_id(fr.node_info.num)} {fr.node_info.user.long_name!r}"
    if which == "queueStatus":
        q = fr.queueStatus
        return f"queueStatus free={q.free}/{q.maxlen} res={q.res}"
    return which or "empty"


def describe_to_radio(tr: mesh_pb2.ToRadio) -> str:
    which = tr.WhichOneof("payload_variant")
    if which == "packet":
        return f"packet {describe_packet(tr.packet)}"
    if which == "want_config_id":
        nonce = tr.want_config_id
        special = {NONCE_ONLY_CONFIG: " (config only)", NONCE_ONLY_DB: " (node db only)"}
        return f"want_config_id={nonce}{special.get(nonce, '')}"
    return which or "empty"
