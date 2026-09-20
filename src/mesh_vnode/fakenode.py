"""A fake Meshtastic node on TCP, for testing without hardware.

It speaks the node side of the protocol: it answers `want_config_id` with a
plausible handshake and then emits text packets on a timer. Enough to exercise
capture, storage, replay and the web UI end to end.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import random
import time

from meshtastic.protobuf import admin_pb2, config_pb2, mesh_pb2, module_config_pb2, telemetry_pb2

from .framing import FrameDecoder, encode_frame

logger = logging.getLogger(__name__)

MY_NUM = 0x433A1B2C
PEERS = [
    (0x11AA22BB, "!11aa22bb", "Hilltop Relay", "RR"),
    (0x33CC44DD, "!33cc44dd", "Kitchen Table", "KT"),
    (0x55EE66FF, "!55ee66ff", "Bike Tracker", "BT"),
]
# Starred on the node, like a real node's favourites.
FAVORITES = {0x11AA22BB, 0x33CC44DD}

Role = config_pb2.Config.DeviceConfig.Role
_rng = random.Random(7)
# A crowd of nodes that are only ever heard, never chatted with - a real node
# here knew 250 of them. Deterministic, so the node list is stable between runs.
CROWD = [
    (
        0x70000000 + i,
        f"!{0x70000000 + i:08x}",
        f"{_rng.choice(['Hilltop', 'Valley', 'Tower', 'Garden', 'Mobile', 'Base', 'Solar'])} "
        f"{_rng.choice(['North', 'South', 'East', 'West', 'Alpha', 'Bravo'])} {i}",
        f"{i:04d}"[-4:],
        _rng.choice([Role.CLIENT, Role.CLIENT, Role.ROUTER, Role.CLIENT_MUTE, Role.TRACKER]),
        _rng.choice([0, 1, 1, 2, 3, 4]),
        _rng.random() < 0.2,
        _rng.randint(60, 7 * 86400),
    )
    for i in range(40)
]


def _from_radio(**kw) -> mesh_pb2.FromRadio:
    fr = mesh_pb2.FromRadio()
    for k, v in kw.items():
        getattr(fr, k).CopyFrom(v)
    return fr


def build_handshake(config_id: int) -> list[bytes]:
    frames: list[mesh_pb2.FromRadio] = []

    fr = mesh_pb2.FromRadio()
    fr.my_info.my_node_num = MY_NUM
    fr.my_info.min_app_version = 30200
    frames.append(fr)

    fr = mesh_pb2.FromRadio()
    fr.metadata.firmware_version = "2.7.4.abcdef"
    fr.metadata.device_state_version = 24
    fr.metadata.hw_model = mesh_pb2.HardwareModel.HELTEC_V3
    frames.append(fr)

    for idx in range(3):
        fr = mesh_pb2.FromRadio()
        fr.channel.index = idx
        if idx == 0:
            fr.channel.role = fr.channel.Role.PRIMARY
            fr.channel.settings.name = ""
            fr.channel.settings.psk = bytes([1])
        elif idx == 1:
            fr.channel.role = fr.channel.Role.SECONDARY
            fr.channel.settings.name = "vnode-test"
            fr.channel.settings.psk = bytes(range(16))
        else:
            fr.channel.role = fr.channel.Role.DISABLED
        frames.append(fr)

    device = config_pb2.Config.DeviceConfig(role=config_pb2.Config.DeviceConfig.Role.CLIENT)
    fr = mesh_pb2.FromRadio()
    fr.config.device.CopyFrom(device)
    frames.append(fr)

    fr = mesh_pb2.FromRadio()
    fr.config.lora.region = config_pb2.Config.LoRaConfig.RegionCode.EU_868
    fr.config.lora.use_preset = True
    fr.config.lora.modem_preset = config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST
    fr.config.lora.hop_limit = 3
    frames.append(fr)

    fr = mesh_pb2.FromRadio()
    fr.moduleConfig.mqtt.CopyFrom(module_config_pb2.ModuleConfig.MQTTConfig(enabled=False))
    frames.append(fr)

    fr = mesh_pb2.FromRadio()
    fr.node_info.num = MY_NUM
    fr.node_info.user.id = "!433a1b2c"
    fr.node_info.user.long_name = "Fake Hilltop"
    fr.node_info.user.short_name = "FAKE"
    fr.node_info.user.hw_model = mesh_pb2.HardwareModel.HELTEC_V3
    fr.node_info.last_heard = int(time.time())
    frames.append(fr)

    for num, nid, long, short in PEERS:
        fr = mesh_pb2.FromRadio()
        fr.node_info.num = num
        fr.node_info.user.id = nid
        fr.node_info.user.long_name = long
        fr.node_info.user.short_name = short
        fr.node_info.user.hw_model = mesh_pb2.HardwareModel.TBEAM
        fr.node_info.user.role = Role.ROUTER if num == 0x11AA22BB else Role.CLIENT
        fr.node_info.last_heard = int(time.time()) - random.randint(60, 3600)
        fr.node_info.snr = round(random.uniform(-10, 10), 2)
        fr.node_info.hops_away = random.randint(0, 2)
        fr.node_info.is_favorite = num in FAVORITES
        fr.node_info.device_metrics.battery_level = random.randint(40, 100)
        fr.node_info.device_metrics.voltage = round(random.uniform(3.6, 4.2), 2)
        frames.append(fr)

    now = int(time.time())
    for num, nid, long, short, role, hops, mqtt, age in CROWD:
        fr = mesh_pb2.FromRadio()
        fr.node_info.num = num
        fr.node_info.user.id = nid
        fr.node_info.user.long_name = long
        fr.node_info.user.short_name = short
        fr.node_info.user.hw_model = mesh_pb2.HardwareModel.HELTEC_V3
        fr.node_info.user.role = role
        fr.node_info.last_heard = now - age
        fr.node_info.hops_away = hops
        fr.node_info.via_mqtt = mqtt
        fr.node_info.snr = round(_rng.uniform(-18, 8), 2)
        frames.append(fr)

    fr = mesh_pb2.FromRadio()
    fr.config_complete_id = config_id
    frames.append(fr)

    return [f.SerializeToString() for f in frames]


class FakeNode:
    def __init__(self, host: str = "127.0.0.1", port: int = 4403, interval: float = 10.0) -> None:
        self.host = host
        self.port = port
        self.interval = interval
        self.clients: set[asyncio.StreamWriter] = set()
        self._ids = itertools.count(random.randint(1000, 9000))

    async def serve(self) -> None:
        server = await asyncio.start_server(self._handle, self.host, self.port)
        logger.info("fakenode: listening on %s:%s", self.host, self.port)
        asyncio.create_task(self._chatter())
        async with server:
            await server.serve_forever()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("fakenode: client %s connected", peer)
        self.clients.add(writer)
        decoder = FrameDecoder()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                for payload in decoder.feed(data):
                    tr = mesh_pb2.ToRadio()
                    tr.ParseFromString(payload)
                    which = tr.WhichOneof("payload_variant")
                    if which == "want_config_id":
                        logger.info("fakenode: handshake for nonce %s", tr.want_config_id)
                        for frame in build_handshake(tr.want_config_id):
                            writer.write(encode_frame(frame))
                        await writer.drain()
                    elif which == "heartbeat":
                        fr = mesh_pb2.FromRadio()
                        fr.queueStatus.free = 16
                        fr.queueStatus.maxlen = 16
                        writer.write(encode_frame(fr.SerializeToString()))
                        await writer.drain()
                    elif which == "packet" and tr.packet.decoded.portnum == 6:
                        admin = admin_pb2.AdminMessage()
                        admin.ParseFromString(tr.packet.decoded.payload)
                        logger.info("fakenode: admin %s", admin.WhichOneof("payload_variant"))
                    elif which == "packet":
                        logger.info(
                            "fakenode: got packet id=%s to=%08x port=%s text=%r",
                            tr.packet.id,
                            tr.packet.to,
                            tr.packet.decoded.portnum if tr.packet.HasField("decoded") else None,
                            tr.packet.decoded.payload[:60]
                            if tr.packet.HasField("decoded")
                            else b"",
                        )
                        # Taken into the transmit queue, like the firmware says.
                        ack = mesh_pb2.FromRadio()
                        ack.queueStatus.free = 15
                        ack.queueStatus.maxlen = 16
                        ack.queueStatus.mesh_packet_id = tr.packet.id
                        writer.write(encode_frame(ack.SerializeToString()))
                        await writer.drain()
                        if tr.packet.want_ack and tr.packet.decoded.portnum == 1:
                            asyncio.create_task(self._answer(writer, tr.packet))
        except Exception as exc:
            logger.debug("fakenode: client error %s", exc)
        finally:
            self.clients.discard(writer)
            with contextlib.suppress(Exception):
                writer.close()
            logger.info("fakenode: client %s gone", peer)

    async def _answer(self, writer: asyncio.StreamWriter, packet: mesh_pb2.MeshPacket) -> None:
        """What the mesh does with a message we sent, on a delay.

        Channel message: another node repeats it, and our node reports that as
        its own (implicit) ack. DM to one of the chatty peers: the recipient
        acks. DM to anyone in the crowd: nobody answers, and the node gives up
        with NO_RESPONSE. The first peer also reacts with a thumbs-up.
        """
        peers = {num for num, *_ in PEERS}
        await asyncio.sleep(1.5)
        # The implicit ack names the relayer by the last byte of its number,
        # with the signal it was overheard at, as the firmware does.
        relayed = self._routing(
            MY_NUM, packet.id, mesh_pb2.Routing.Error.NONE, relay_node=PEERS[0][0] & 0xFF
        )
        if packet.to == 0xFFFFFFFF:
            frames = [relayed]
        elif packet.to in peers:
            frames = [
                relayed,
                self._routing(packet.to, packet.id, mesh_pb2.Routing.Error.NONE),
            ]
        else:
            await asyncio.sleep(1.5)
            frames = [self._routing(MY_NUM, packet.id, mesh_pb2.Routing.Error.NO_RESPONSE)]

        reaction = mesh_pb2.FromRadio()
        rp = reaction.packet
        rp.__setattr__("from", PEERS[0][0])
        rp.to = packet.to if packet.to == 0xFFFFFFFF else MY_NUM
        rp.id = next(self._ids)
        rp.rx_time = int(time.time())
        rp.decoded.portnum = 1
        rp.decoded.payload = "👍".encode()
        rp.decoded.emoji = 1
        rp.decoded.reply_id = packet.id
        frames.append(reaction.SerializeToString())

        for frame in frames:
            with contextlib.suppress(Exception):
                writer.write(encode_frame(frame))
                await writer.drain()
            await asyncio.sleep(0.5)

    def _routing(self, frm: int, request_id: int, error: int, relay_node: int = 0) -> bytes:
        fr = mesh_pb2.FromRadio()
        fr.packet.__setattr__("from", frm)
        if relay_node:
            fr.packet.relay_node = relay_node
            fr.packet.rx_snr = round(random.uniform(-5, 9), 2)
            fr.packet.rx_rssi = random.randint(-110, -70)
        fr.packet.to = MY_NUM
        fr.packet.id = next(self._ids)
        fr.packet.rx_time = int(time.time())
        fr.packet.decoded.portnum = 5
        fr.packet.decoded.request_id = request_id
        fr.packet.decoded.payload = mesh_pb2.Routing(error_reason=error).SerializeToString()
        return fr.SerializeToString()

    async def _chatter(self) -> None:
        counter = 0
        while True:
            await asyncio.sleep(self.interval)
            counter += 1
            for frame in self._make_traffic(counter):
                for w in list(self.clients):
                    try:
                        w.write(encode_frame(frame))
                    except Exception:
                        self.clients.discard(w)

    def _make_traffic(self, counter: int) -> list[bytes]:
        num, _, long, _ = random.choice(PEERS)
        out = []

        fr = mesh_pb2.FromRadio()
        pkt = fr.packet
        pkt.__setattr__("from", num)
        dm = counter % 3 == 0
        pkt.to = MY_NUM if dm else 0xFFFFFFFF
        pkt.id = next(self._ids)
        pkt.channel = 0
        pkt.rx_time = int(time.time())
        pkt.rx_snr = round(random.uniform(-12, 10), 2)
        pkt.rx_rssi = random.randint(-120, -40)
        pkt.hop_limit = 3
        pkt.hop_start = 3
        pkt.decoded.portnum = 1
        kind = "DM" if dm else "broadcast"
        pkt.decoded.payload = f"{kind} #{counter} from {long}".encode()
        out.append(fr.SerializeToString())

        out.extend(self._local_traffic(counter))

        if counter % 2 == 0:
            fr = mesh_pb2.FromRadio()
            pkt = fr.packet
            pkt.__setattr__("from", num)
            pkt.to = 0xFFFFFFFF
            pkt.id = next(self._ids)
            pkt.rx_time = int(time.time())
            pkt.decoded.portnum = 67
            tel = telemetry_pb2.Telemetry()
            tel.time = int(time.time())
            tel.device_metrics.battery_level = random.randint(30, 100)
            tel.device_metrics.voltage = round(random.uniform(3.5, 4.2), 2)
            tel.device_metrics.channel_utilization = round(random.uniform(0, 30), 2)
            tel.device_metrics.air_util_tx = round(random.uniform(0, 10), 2)
            tel.device_metrics.uptime_seconds = counter * 60
            pkt.decoded.payload = tel.SerializeToString()
            out.append(fr.SerializeToString())

        # Position chatter from the crowd: counted in traffic, never stored.
        num = random.choice(CROWD)[0]
        fr = mesh_pb2.FromRadio()
        fr.packet.__setattr__("from", num)
        fr.packet.to = 0xFFFFFFFF
        fr.packet.id = next(self._ids)
        fr.packet.rx_time = int(time.time())
        fr.packet.decoded.portnum = 3
        pos = mesh_pb2.Position(
            latitude_i=514769000 + random.randint(-900000, 900000),
            longitude_i=-5000 + random.randint(-900000, 900000),
            # Most nodes share a blurred position; the map draws the blur.
            precision_bits=random.choice([32, 16, 14, 13, 13]),
        )
        fr.packet.decoded.payload = pos.SerializeToString()
        out.append(fr.SerializeToString())
        return out

    def _local_packet(self, portnum: int, payload: bytes) -> bytes:
        fr = mesh_pb2.FromRadio()
        fr.packet.__setattr__("from", MY_NUM)
        fr.packet.to = 0xFFFFFFFF
        fr.packet.id = next(self._ids)
        fr.packet.rx_time = int(time.time())
        fr.packet.decoded.portnum = portnum
        fr.packet.decoded.payload = payload
        return fr.SerializeToString()

    def _local_traffic(self, counter: int) -> list[bytes]:
        """What a real node reports about itself: position with fix quality every
        minute or so, device metrics, and local stats (noise floor, counters)."""
        out = []
        pos = mesh_pb2.Position(
            latitude_i=514769000,  # Greenwich Observatory: a public, made-up location
            longitude_i=-5000,
            altitude=101,
            time=int(time.time()),
            sats_in_view=random.randint(6, 12),
            PDOP=random.randint(110, 260),
            precision_bits=32,
        )
        out.append(self._local_packet(3, pos.SerializeToString()))

        tel = telemetry_pb2.Telemetry(time=int(time.time()))
        tel.device_metrics.battery_level = 101
        tel.device_metrics.voltage = round(random.uniform(4.3, 4.4), 3)
        tel.device_metrics.channel_utilization = round(random.uniform(8, 22), 2)
        tel.device_metrics.air_util_tx = round(random.uniform(1, 4), 2)
        tel.device_metrics.uptime_seconds = 273000 + counter * 60
        out.append(self._local_packet(67, tel.SerializeToString()))

        tel = telemetry_pb2.Telemetry(time=int(time.time()))
        stats = tel.local_stats
        stats.uptime_seconds = 273000 + counter * 60
        stats.channel_utilization = round(random.uniform(8, 22), 2)
        stats.air_util_tx = round(random.uniform(1, 4), 2)
        stats.noise_floor = random.randint(-112, -98)
        stats.num_packets_rx = 5000 + counter * random.randint(20, 40)
        stats.num_packets_tx = 800 + counter * random.randint(2, 6)
        stats.num_packets_rx_bad = 40 + counter
        stats.num_rx_dupe = 900 + counter * 8
        stats.num_tx_relay = 300 + counter * 3
        stats.num_online_nodes = random.randint(55, 70)
        stats.num_total_nodes = 250
        out.append(self._local_packet(67, tel.SerializeToString()))
        return out
