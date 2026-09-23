"""End-to-end over a real socket: a fake client against the virtual node.

These are the tests that matter - the handshake order and the replay cursor are
what an app actually depends on. The handshake expectations follow the firmware:
https://github.com/meshtastic/firmware/blob/3468af94aa0f79e93c9bf041244bf230161fc704/src/mesh/PhoneAPI.cpp
"""

import asyncio
import threading
import time

import pytest
from meshtastic.protobuf import mesh_pb2

from mesh_vnode import protocol as proto
from mesh_vnode.config import Settings
from mesh_vnode.db import Database
from mesh_vnode.framing import FrameDecoder, encode_frame
from mesh_vnode.vnode_server import VNodeServer

MY_NUM = 0x433A1B2C


class FakeUpstream:
    """Stands in for the real node: records what was sent, stores what it is told."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.sent: list[bytes] = []
        self.my_node_num = MY_NUM
        self.raw_sinks: list = []
        self.last_error = None
        self.connected = threading.Event()
        self.connected.set()
        self.refreshes = 0

    def refresh_config_soon(self) -> None:
        self.refreshes += 1

    def add_raw_sink(self, sink) -> None:
        self.raw_sinks.append(sink)

    def send_to_radio_bytes(self, payload: bytes) -> None:
        self.sent.append(payload)

    def store_client_packet(self, packet, origin):
        return self.db.store_packet(
            raw=proto.wrap_packet(packet, override_from=MY_NUM),
            meta={**proto.packet_meta(packet), "origin": origin, "rx_time": int(time.time())},
        )


# FromRadio field 19, region_presets: new in firmware 2.8 and unknown to the
# meshtastic package's protobufs. Built by hand, as the node would send it.
REGION_PRESETS_FRAME = bytes([0x9A, 0x01, 0x02, 0x08, 0x01])


def seed_config(db: Database) -> None:
    """A captured handshake in the order firmware 2.8.1 sends it."""
    frames = []

    def add(key, kind, raw):
        frames.append((key, kind, len(frames), raw))

    fr = mesh_pb2.FromRadio()
    fr.my_info.my_node_num = MY_NUM
    add("my_info", "my_info", fr.SerializeToString())

    fr = mesh_pb2.FromRadio()
    fr.node_info.num = MY_NUM
    fr.node_info.user.long_name = "Me"
    fr.node_info.is_favorite = True
    add(f"node_info:{MY_NUM}", "node_info", fr.SerializeToString())

    fr = mesh_pb2.FromRadio()
    fr.metadata.firmware_version = "2.8.1"
    add("metadata", "metadata", fr.SerializeToString())

    add("unknown:3", "other", REGION_PRESETS_FRAME)

    fr = mesh_pb2.FromRadio()
    fr.channel.index = 0
    fr.channel.role = fr.channel.Role.PRIMARY
    add("channel:0", "channel", fr.SerializeToString())

    fr = mesh_pb2.FromRadio()
    fr.config.lora.hop_limit = 3
    add("config:lora", "config", fr.SerializeToString())

    fr = mesh_pb2.FromRadio()
    fr.node_info.num = 0x11AA22BB
    fr.node_info.user.long_name = "Peer"
    add(f"node_info:{0x11AA22BB}", "node_info", fr.SerializeToString())

    db.replace_config_frames(frames)


def seed_texts(db: Database, count: int, start_id: int = 100) -> None:
    for i in range(count):
        pkt = mesh_pb2.MeshPacket()
        pkt.__setattr__("from", 0x11AA22BB)
        pkt.to = proto.BROADCAST_NUM
        pkt.id = start_id + i
        pkt.rx_time = int(time.time())
        pkt.decoded.portnum = proto.PORT_TEXT
        pkt.decoded.payload = f"stored message {i}".encode()
        db.store_packet(raw=proto.wrap_packet(pkt), meta=proto.packet_meta(pkt))


class Client:
    """A minimal stand-in for the phone app."""

    def __init__(self, reader, writer) -> None:
        self.reader, self.writer = reader, writer
        self.decoder = FrameDecoder()
        self.backlog: list[mesh_pb2.FromRadio] = []

    async def send(self, to_radio: mesh_pb2.ToRadio) -> None:
        self.writer.write(encode_frame(to_radio.SerializeToString()))
        await self.writer.drain()

    async def want_config(self, nonce: int) -> None:
        tr = mesh_pb2.ToRadio()
        tr.want_config_id = nonce
        await self.send(tr)

    async def _frames(self, timeout: float) -> list[mesh_pb2.FromRadio] | None:
        """What the next read brings, [] if it timed out, None at EOF."""
        try:
            data = await asyncio.wait_for(self.reader.read(4096), timeout)
        except TimeoutError:
            return []
        if not data:
            return None
        return [mesh_pb2.FromRadio.FromString(p) for p in self.decoder.feed(data)]

    async def collect(self, seconds: float = 1.2, idle: float = 0.2) -> list[mesh_pb2.FromRadio]:
        """Frames until `idle` seconds pass without one, or `seconds` in all."""
        out, self.backlog = self.backlog, []
        deadline = time.monotonic() + seconds
        quiet_since = time.monotonic()
        while time.monotonic() < deadline:
            frames = await self._frames(0.05)
            if frames is None:
                break
            if frames:
                out += frames
                quiet_since = time.monotonic()
            elif out and time.monotonic() - quiet_since > idle:
                break
        return out

    async def settle(self) -> None:
        """Wait until everything sent so far has been handled. The server takes
        a client's frames in order, so a heartbeat's answer marks the point."""
        tr = mesh_pb2.ToRadio()
        tr.heartbeat.SetInParent()
        await self.send(tr)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            frames = await self._frames(0.05)
            if frames is None:
                return
            for fr in frames:
                if (
                    fr.WhichOneof("payload_variant") == "queueStatus"
                    and not fr.queueStatus.mesh_packet_id
                ):
                    return
                self.backlog.append(fr)
        raise TimeoutError("no answer to the heartbeat")

    async def close(self) -> None:
        self.writer.close()


def kinds(frames) -> list[str]:
    return [f.WhichOneof("payload_variant") or "unknown" for f in frames]


def texts(frames) -> list[str]:
    return [
        f.packet.decoded.payload.decode()
        for f in frames
        if f.WhichOneof("payload_variant") == "packet"
        and f.packet.HasField("decoded")
        and f.packet.decoded.portnum == proto.PORT_TEXT
    ]


@pytest.fixture
async def running(tmp_path, request):
    overrides = getattr(request, "param", {})
    settings = Settings(
        db_path=tmp_path / "vnode.sqlite3",
        listen_host="127.0.0.1",
        listen_port=0,
        replay_pace_ms=0,
        client_key_mode="shared",  # one key, so a reconnect resumes in tests
        **overrides,
    )
    db = Database(settings.db_path)
    seed_config(db)
    upstream = FakeUpstream(db)
    server = VNodeServer(settings, db, upstream)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]

    async def connect() -> Client:
        return Client(*await asyncio.open_connection("127.0.0.1", port))

    yield server, db, upstream, connect
    await server.stop()
    db.close()


def node_nums(frames) -> list[int]:
    return [f.node_info.num for f in frames if f.WhichOneof("payload_variant") == "node_info"]


async def test_handshake_follows_the_firmware_order(running):
    _, _, _, connect = running
    client = await connect()
    await client.want_config(1234)
    frames = await client.collect()

    assert kinds(frames) == [
        "my_info",
        "node_info",
        "metadata",
        "unknown",
        "channel",
        "config",
        "node_info",
        "config_complete_id",
    ]
    assert frames[-1].config_complete_id == 1234
    await client.close()


async def test_a_frame_newer_than_the_package_is_replayed_byte_for_byte(running):
    """region_presets: the package cannot parse it, the app can."""
    _, _, _, connect = running
    client = await connect()
    await client.want_config(1)
    frames = await client.collect()
    unknown = [f for f in frames if f.WhichOneof("payload_variant") is None]
    assert [f.SerializeToString() for f in unknown] == [REGION_PRESETS_FRAME]
    await client.close()


async def test_nonce_69420_keeps_our_own_node_and_drops_the_others(running):
    """PhoneAPI.cpp sends the own node info in every phase that is not 69421-only."""
    _, _, _, connect = running
    client = await connect()
    await client.want_config(proto.NONCE_ONLY_CONFIG)
    frames = await client.collect()
    assert node_nums(frames) == [MY_NUM]
    assert kinds(frames)[0] == "my_info"
    await client.close()


async def test_nonce_69421_sends_our_node_then_the_others_and_no_my_info(running):
    """PhoneAPI.cpp: ONLY_NODES starts at STATE_SEND_OWN_NODEINFO."""
    _, _, _, connect = running
    client = await connect()
    await client.want_config(proto.NONCE_ONLY_DB)
    frames = await client.collect()
    assert kinds(frames) == ["node_info", "node_info", "config_complete_id"]
    assert node_nums(frames) == [MY_NUM, 0x11AA22BB]
    await client.close()


async def test_missed_messages_are_replayed_after_config_complete(running):
    _, db, _, connect = running
    seed_texts(db, 3)

    client = await connect()
    await client.want_config(1)
    frames = await client.collect()

    assert texts(frames) == ["stored message 0", "stored message 1", "stored message 2"]
    # The default position puts the backlog after config_complete, matching the
    # firmware, which only releases queued packets once the client is configured.
    complete_at = kinds(frames).index("config_complete_id")
    first_text = next(i for i, k in enumerate(kinds(frames)) if k == "packet")
    assert first_text > complete_at
    await client.close()


async def test_replayed_bytes_are_identical_to_what_was_stored(running):
    """The whole point: no rebuilt packets, so hop counts stay truthful."""
    _, db, _, connect = running
    seed_texts(db, 1)
    stored = db.packets_after(0, limit=1)[0]["raw"]

    client = await connect()
    await client.want_config(1)
    frames = await client.collect()
    replayed = [f for f in frames if f.WhichOneof("payload_variant") == "packet"]
    assert replayed[0].SerializeToString() == stored
    await client.close()


async def test_a_reconnect_only_replays_what_is_new(running):
    _, db, _, connect = running
    seed_texts(db, 2)

    first = await connect()
    await first.want_config(1)
    assert len(texts(await first.collect())) == 2
    await first.settle()
    await first.close()

    seed_texts(db, 1, start_id=900)
    second = await connect()
    await second.want_config(2)
    assert texts(await second.collect()) == ["stored message 0"]
    await second.close()


async def test_nothing_new_means_nothing_replayed(running):
    _, db, _, connect = running
    seed_texts(db, 2)

    first = await connect()
    await first.want_config(1)
    await first.collect()
    await first.settle()
    await first.close()

    second = await connect()
    await second.want_config(2)
    assert texts(await second.collect()) == []
    await second.close()


@pytest.mark.parametrize("running", [{"replay_mode": "none"}], indirect=True)
async def test_replay_can_be_turned_off(running):
    _, db, _, connect = running
    seed_texts(db, 3)

    client = await connect()
    await client.want_config(1)
    assert texts(await client.collect()) == []
    await client.close()


async def test_the_backlog_arrives_after_config_complete(running):
    _, db, _, connect = running
    seed_texts(db, 1)

    client = await connect()
    await client.want_config(1)
    got = kinds(await client.collect())
    assert got.index("config_complete_id") < got.index("packet")
    await client.close()


@pytest.mark.parametrize("running", [{"replay_limit": 2}], indirect=True)
async def test_a_backlog_over_the_limit_yields_the_newest_slice(running):
    """Taking the oldest slice would advance the cursor past the tail and lose
    everything in between."""
    _, db, _, connect = running
    seed_texts(db, 5)

    client = await connect()
    await client.want_config(1)
    assert texts(await client.collect()) == ["stored message 3", "stored message 4"]
    await client.close()


async def test_heartbeat_gets_a_queue_status(running):
    _, _, _, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)

    tr = mesh_pb2.ToRadio()
    tr.heartbeat.CopyFrom(mesh_pb2.Heartbeat())
    await client.send(tr)
    assert "queueStatus" in kinds(await client.collect(0.5))
    await client.close()


async def test_disconnect_is_handled_locally(running):
    _, _, upstream, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)

    tr = mesh_pb2.ToRadio()
    tr.disconnect = True
    await client.send(tr)
    await client.settle()
    # Never forwarded: the shared upstream session must survive an app closing.
    assert upstream.sent == []
    await client.close()


async def test_outgoing_text_is_forwarded_and_stored(running):
    _, db, upstream, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)

    tr = mesh_pb2.ToRadio()
    tr.packet.to = proto.BROADCAST_NUM
    tr.packet.id = 777
    tr.packet.decoded.portnum = proto.PORT_TEXT
    tr.packet.decoded.payload = b"from the phone"
    await client.send(tr)
    await client.settle()

    assert len(upstream.sent) == 1
    assert db.messages(limit=5)[0]["text"] == "from the phone"
    await client.close()


async def test_a_traceroute_from_the_app_is_recorded_as_a_request(running):
    """Being the connection the app talks through is what makes this visible:
    its answer comes back addressed to our node and lands in the same row."""
    _, db, upstream, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)

    tr = mesh_pb2.ToRadio()
    tr.packet.to = 0x11AA22BB
    tr.packet.id = 778
    tr.packet.channel = 2
    tr.packet.decoded.portnum = proto.PORT_TRACEROUTE
    tr.packet.decoded.want_response = True
    await client.send(tr)
    await client.settle()

    row = db.exchanges(0x11AA22BB)[0]
    assert (row["kind"], row["direction"], row["status"]) == ("traceroute", "out", "sent")
    assert (row["packet_id"], row["channel"]) == (778, 2)
    assert row["origin"] and row["origin"] != "webui"
    assert len(upstream.sent) == 1  # and it still went upstream untouched
    await client.close()


async def test_a_plain_message_from_the_app_is_not_a_request(running):
    _, db, _, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)

    tr = mesh_pb2.ToRadio()
    tr.packet.to = 0x11AA22BB
    tr.packet.id = 779
    tr.packet.decoded.portnum = proto.PORT_TEXT
    tr.packet.decoded.payload = b"just talking"
    await client.send(tr)
    await client.settle()

    assert db.exchanges() == []
    await client.close()


async def test_a_clients_own_message_is_not_replayed_back_to_it(running):
    _, _, _, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)

    tr = mesh_pb2.ToRadio()
    tr.packet.to = proto.BROADCAST_NUM
    tr.packet.id = 778
    tr.packet.decoded.portnum = proto.PORT_TEXT
    tr.packet.decoded.payload = b"mine"
    await client.send(tr)
    await client.settle()
    await client.close()

    again = await connect()
    await again.want_config(2)
    assert texts(await again.collect()) == []
    await again.close()


async def test_admin_packets_are_blocked_by_default(running):
    _, _, upstream, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)

    tr = mesh_pb2.ToRadio()
    tr.packet.to = MY_NUM
    tr.packet.id = 779
    tr.packet.decoded.portnum = proto.PORT_ADMIN
    tr.packet.decoded.payload = b"\x01"
    await client.send(tr)
    await client.settle()

    assert upstream.sent == []
    await client.close()


@pytest.mark.parametrize("running", [{"allow_admin": True}], indirect=True)
async def test_admin_packets_pass_when_allowed(running):
    _, _, upstream, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)

    tr = mesh_pb2.ToRadio()
    tr.packet.to = MY_NUM
    tr.packet.id = 780
    tr.packet.decoded.portnum = proto.PORT_ADMIN
    tr.packet.decoded.payload = b"\x01"
    await client.send(tr)
    await client.settle()

    assert len(upstream.sent) == 1
    await client.close()


async def test_live_traffic_reaches_a_configured_client(running):
    server, db, _, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)

    pkt = mesh_pb2.MeshPacket()
    pkt.__setattr__("from", 0x11AA22BB)
    pkt.to = proto.BROADCAST_NUM
    pkt.id = 555
    pkt.rx_time = int(time.time())
    pkt.decoded.portnum = proto.PORT_TEXT
    pkt.decoded.payload = b"live one"
    raw = proto.wrap_packet(pkt)
    seq = db.store_packet(raw=raw, meta=proto.packet_meta(pkt))
    server._fanout(raw, seq, "packet")

    assert texts(await client.collect(0.5)) == ["live one"]
    await client.close()


async def test_a_repeat_config_request_is_ignored_within_the_cooldown(running):
    _, _, _, connect = running
    client = await connect()
    await client.want_config(4242)
    assert kinds(await client.collect(0.6)).count("config_complete_id") == 1

    await client.want_config(4242)
    assert kinds(await client.collect(0.5)).count("config_complete_id") == 0
    await client.close()


async def test_a_second_client_sees_what_the_first_one_sent(running):
    _, _, _, connect = running
    first = await connect()
    await first.want_config(1)
    await first.collect(0.4)

    second = await connect()
    await second.want_config(2)
    await second.collect(0.4)

    tr = mesh_pb2.ToRadio()
    tr.packet.to = proto.BROADCAST_NUM
    tr.packet.id = 881
    tr.packet.decoded.portnum = proto.PORT_TEXT
    tr.packet.decoded.payload = b"hello other phone"
    await first.send(tr)

    assert texts(await second.collect(0.6)) == ["hello other phone"]
    await first.close()
    await second.close()


def admin_packet(**variant) -> mesh_pb2.ToRadio:
    from meshtastic.protobuf import admin_pb2

    msg = admin_pb2.AdminMessage(**variant)
    tr = mesh_pb2.ToRadio()
    tr.packet.to = MY_NUM
    tr.packet.id = 990
    tr.packet.decoded.portnum = proto.PORT_ADMIN
    tr.packet.decoded.payload = msg.SerializeToString()
    return tr


class FlagRecordingUpstream(FakeUpstream):
    def __init__(self, db):
        super().__init__(db)
        self.flags: list = []

    def note_node_flags(self, node_num, **flags):
        self.flags.append((node_num, flags))


async def test_starring_a_node_in_the_app_passes_the_admin_block(running):
    server, _, upstream, connect = running
    recorder = FlagRecordingUpstream(upstream.db)
    server.upstream = recorder

    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)
    await client.send(admin_packet(set_favorite_node=0x11AA22BB))
    await client.settle()

    assert len(recorder.sent) == 1
    assert recorder.flags == [(0x11AA22BB, {"is_favorite": True})]
    await client.close()


async def test_other_admin_messages_stay_blocked(running):
    server, _, upstream, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)
    await client.send(admin_packet(reboot_seconds=5))
    await client.settle()

    assert upstream.sent == []
    await client.close()


async def test_a_star_is_not_recorded_when_the_node_is_down(running):
    server, _, upstream, connect = running
    recorder = FlagRecordingUpstream(upstream.db)
    recorder.connected.clear()
    server.upstream = recorder

    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)
    await client.send(admin_packet(set_favorite_node=0x11AA22BB))
    await client.settle()

    # The node never heard about it, so this service must not pretend it did.
    assert recorder.flags == []
    await client.close()


async def test_a_read_only_admin_request_passes(running):
    """What the app asks for right after connecting, e.g. its session key."""
    from meshtastic.protobuf import admin_pb2

    _, _, upstream, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)
    await client.send(
        admin_packet(get_config_request=admin_pb2.AdminMessage.ConfigType.SESSIONKEY_CONFIG)
    )
    await client.settle()

    assert len(upstream.sent) == 1
    # Byte-for-byte: no sender fix-up on admin traffic.
    assert (
        upstream.sent[0]
        == admin_packet(
            get_config_request=admin_pb2.AdminMessage.ConfigType.SESSIONKEY_CONFIG
        ).SerializeToString()
    )
    await client.close()


async def test_an_admin_write_is_still_blocked(running):
    _, _, upstream, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)
    await client.send(admin_packet(set_owner=mesh_pb2.User(long_name="pwned")))
    await client.settle()

    assert upstream.sent == []
    await client.close()


async def test_a_blocked_admin_write_is_answered_with_a_nak(running):
    """Otherwise the app sits waiting for a reply that never comes."""
    _, _, _, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)
    await client.send(admin_packet(set_owner=mesh_pb2.User(long_name="new name")))
    [reply] = [f for f in await client.collect(0.5) if f.WhichOneof("payload_variant") == "packet"]
    assert reply.packet.decoded.portnum == proto.PORT_ROUTING
    assert reply.packet.decoded.request_id == 990
    routing = mesh_pb2.Routing.FromString(reply.packet.decoded.payload)
    assert routing.error_reason == mesh_pb2.Routing.Error.NOT_AUTHORIZED
    await client.close()


@pytest.mark.parametrize("running", [{"allow_admin": True}], indirect=True)
async def test_an_allowed_admin_write_refreshes_the_stored_config(running):
    _, _, upstream, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)
    await client.send(admin_packet(set_owner=mesh_pb2.User(long_name="new name")))
    await client.settle()
    assert len(upstream.sent) == 1
    assert upstream.refreshes == 1
    await client.close()


async def test_a_read_does_not_refresh_the_config(running):
    _, _, upstream, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)
    await client.send(admin_packet(get_owner_request=True))
    await client.settle()
    assert upstream.refreshes == 0
    await client.close()


@pytest.mark.parametrize("running", [{"allow_admin_reads": False}], indirect=True)
async def test_reads_can_be_blocked_too(running):
    _, _, upstream, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)
    await client.send(admin_packet(get_owner_request=True))
    await client.settle()

    assert upstream.sent == []
    await client.close()


async def test_disconnect_all_drops_every_app(running):
    server, _, _, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.3)
    assert server.disconnect_all() == 1
    assert server.clients == {}
    assert await client.reader.read(100) == b""  # the app sees the connection close
    await client.close()


async def test_stopping_does_not_wait_for_connected_apps(running):
    server, _, _, connect = running
    client = await connect()
    await client.want_config(1)
    await client.collect(0.3)
    await asyncio.wait_for(server.stop(), 2)
    assert not server.clients
    assert await client.reader.read() == b""
