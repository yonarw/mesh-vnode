"""What gets stored from a busy mesh, and what does not.

A real node here knew 250 nodes and heard ~75 position/telemetry packets a
minute. Only text is kept raw; telemetry only for this node and its favourites.
"""

import sqlite3
import time

import pytest
from meshtastic.protobuf import config_pb2, mesh_pb2, telemetry_pb2

from mesh_vnode import protocol as proto
from mesh_vnode.config import Settings
from mesh_vnode.db import Database
from mesh_vnode.upstream import Upstream

ME = 0x433A1B2C
FAV = 0x11AA22BB
STRANGER = 0x70000001


@pytest.fixture
def up(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    frames = []
    fr = mesh_pb2.FromRadio()
    fr.my_info.my_node_num = ME
    frames.append(("my_info", "my_info", 0, fr.SerializeToString()))
    for num, fav in ((ME, False), (FAV, True), (STRANGER, False)):
        fr = mesh_pb2.FromRadio()
        fr.node_info.num = num
        fr.node_info.is_favorite = fav
        frames.append((f"node_info:{num}", "node_info", len(frames), fr.SerializeToString()))
    db.replace_config_frames(frames)
    for num, fav in ((ME, 0), (FAV, 1), (STRANGER, 0)):
        db.upsert_node(num, {"is_favorite": fav})
    upstream = Upstream(Settings(db_path=tmp_path / "t.sqlite3"), db)
    yield upstream
    db.close()


def packet(frm: int, portnum: int, payload: bytes) -> mesh_pb2.MeshPacket:
    pkt = mesh_pb2.MeshPacket()
    pkt.__setattr__("from", frm)
    pkt.to = proto.BROADCAST_NUM
    pkt.id = int(time.time() * 1000) & 0xFFFFFFF ^ frm
    pkt.rx_time = int(time.time())
    pkt.decoded.portnum = portnum
    pkt.decoded.payload = payload
    return pkt


def device_telemetry(battery: int = 80) -> bytes:
    tel = telemetry_pb2.Telemetry()
    tel.device_metrics.battery_level = battery
    tel.device_metrics.voltage = 4.0
    return tel.SerializeToString()


def store(up, pkt):
    fr = mesh_pb2.FromRadio()
    fr.packet.CopyFrom(pkt)
    return up._store_packet(pkt, fr.SerializeToString())


def store_from_client(up, pkt):
    fr = mesh_pb2.FromRadio()
    fr.packet.CopyFrom(pkt)
    return up._store_packet(pkt, fr.SerializeToString(), origin="phone")


def test_telemetry_is_kept_for_this_node(up):
    store(up, packet(ME, proto.PORT_TELEMETRY, device_telemetry()))
    assert up.db.counts()["telemetry"] == 1


def test_telemetry_is_kept_for_favourites(up):
    store(up, packet(FAV, proto.PORT_TELEMETRY, device_telemetry()))
    assert up.db.counts()["telemetry"] == 1


def test_telemetry_from_anyone_else_is_dropped(up):
    store(up, packet(STRANGER, proto.PORT_TELEMETRY, device_telemetry(battery=33)))
    assert up.db.counts()["telemetry"] == 0
    # ...but the node list still learns the battery level.
    assert up.db.node(STRANGER)["battery_level"] == 33


def test_only_text_is_stored_raw(up):
    assert store(up, packet(STRANGER, proto.PORT_TELEMETRY, device_telemetry())) is None
    pos = mesh_pb2.Position(latitude_i=1, longitude_i=1).SerializeToString()
    assert store(up, packet(STRANGER, proto.PORT_POSITION, pos)) is None
    assert store(up, packet(STRANGER, proto.PORT_TEXT, b"hi")) is not None
    assert up.db.counts()["packets"] == 1


def test_every_packet_is_counted_in_traffic(up):
    store(up, packet(STRANGER, proto.PORT_TELEMETRY, device_telemetry()))
    store(up, packet(STRANGER, proto.PORT_TEXT, b"hi"))
    store(up, packet(FAV, proto.PORT_TEXT, b"yo"))
    counts = {r["portnum"]: r["count"] for r in up.db.traffic(0)}
    assert counts == {proto.PORT_TELEMETRY: 1, proto.PORT_TEXT: 2}


def test_a_text_message_marks_the_sender_as_heard(up):
    """A node that only chats would otherwise look days stale in the node list,
    while the handshake replayed to the phone said it was heard just now."""
    pkt = packet(STRANGER, proto.PORT_TEXT, b"hi")
    store(up, pkt)
    assert up.db.node(STRANGER)["last_heard"] == pkt.rx_time


def test_our_own_outgoing_text_does_not_mark_us_as_heard(up):
    pkt = packet(ME, proto.PORT_TEXT, b"hi")
    store_from_client(up, pkt)
    assert up.db.node(ME)["last_heard"] is None


def test_gps_quality_is_recorded_from_this_nodes_position(up):
    pos = mesh_pb2.Position(
        latitude_i=514769000, longitude_i=-5000, sats_in_view=9, PDOP=141, precision_bits=32
    )
    store(up, packet(ME, proto.PORT_POSITION, pos.SerializeToString()))
    rows = up.db.telemetry_rows(ME, 0)
    assert len(rows) == 1
    assert rows[0]["kind"] == "gps"
    assert rows[0]["sats_in_view"] == 9
    assert rows[0]["pdop"] == 1.41
    assert rows[0]["precision_bits"] == 32


def test_local_stats_carry_the_noise_floor(up):
    tel = telemetry_pb2.Telemetry()
    tel.local_stats.noise_floor = -104
    tel.local_stats.num_packets_rx = 5000
    tel.local_stats.num_online_nodes = 61
    store(up, packet(ME, proto.PORT_TELEMETRY, tel.SerializeToString()))
    row = up.db.telemetry_rows(ME, 0)[0]
    assert row["kind"] == "local"
    assert row["noise_floor"] == -104
    assert row["num_packets_rx"] == 5000
    assert row["num_online_nodes"] == 61


def test_starring_a_node_starts_tracking_its_telemetry(up):
    store(up, packet(STRANGER, proto.PORT_TELEMETRY, device_telemetry()))
    assert up.db.counts()["telemetry"] == 0
    up.note_node_flags(STRANGER, is_favorite=True)
    store(up, packet(STRANGER, proto.PORT_TELEMETRY, device_telemetry(battery=50)))
    assert up.db.counts()["telemetry"] == 1


def test_a_star_change_is_patched_into_the_replayed_handshake(up):
    """Otherwise a phone keeps seeing the old star until the node reconnects."""
    up.note_node_flags(STRANGER, is_favorite=True)
    row = next(f for f in up.db.config_frames() if f["key"] == f"node_info:{STRANGER}")
    assert proto.parse_from_radio(row["raw"]).node_info.is_favorite is True


def test_node_roles_decode(up):
    """Found against a real node: the role enum lives in config_pb2."""
    info = mesh_pb2.NodeInfo(num=STRANGER)
    info.user.role = config_pb2.Config.DeviceConfig.Role.ROUTER
    up._store_nodeinfo_frame(info)
    assert up.db.node(STRANGER)["role"] == "ROUTER"

    user = mesh_pb2.User(role=config_pb2.Config.DeviceConfig.Role.ROUTER)
    pkt = packet(STRANGER, proto.PORT_NODEINFO, user.SerializeToString())
    assert proto.decode_nodeinfo(pkt)["role"] == "ROUTER"


def test_a_version_1_database_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (node_num INTEGER PRIMARY KEY, long_name TEXT,
                            updated_at INTEGER NOT NULL);
        INSERT INTO nodes VALUES (1, 'old', 0);
        CREATE TABLE packets (seq INTEGER PRIMARY KEY AUTOINCREMENT, packet_id INTEGER NOT NULL,
            from_num INTEGER NOT NULL, to_num INTEGER NOT NULL, channel INTEGER NOT NULL DEFAULT 0,
            portnum INTEGER NOT NULL, rx_time INTEGER NOT NULL, stored_at INTEGER NOT NULL,
            text TEXT, want_ack INTEGER NOT NULL DEFAULT 0, hop_limit INTEGER NOT NULL DEFAULT 0,
            hop_start INTEGER NOT NULL DEFAULT 0, rx_snr REAL, rx_rssi INTEGER,
            pki INTEGER NOT NULL DEFAULT 0, origin TEXT, raw BLOB NOT NULL);
        INSERT INTO packets(packet_id, from_num, to_num, portnum, rx_time, stored_at, raw)
            VALUES (1, 1, 2, 67, 0, 0, x'00'), (2, 1, 2, 1, 0, 0, x'00');
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    node = db.node(1)
    assert node["long_name"] == "old"
    assert node["is_favorite"] == 0
    # Non-text packets from version 1 are dropped; text survives.
    assert [r["portnum"] for r in db.packets_after(0, limit=10)] == [1]
    db.close()


def test_precision_bits_translate_to_distance():
    assert proto.precision_bits_meters(32) == 0.0
    assert 2800 < proto.precision_bits_meters(13) < 3000
    assert proto.precision_bits_meters(0) is None


def test_a_handshake_frame_the_package_cannot_name_is_captured(up):
    """region_presets arrives mid-handshake; losing it would change what the
    app sees compared with talking to the node directly."""
    iface = type("I", (), {"_capturing": True, "_captured": [], "_config_ord": 5})()
    raw = bytes([0x9A, 0x01, 0x02, 0x08, 0x01])
    up._on_raw_frame(raw, iface)
    assert iface._captured == [("unknown:5", "other", 5, raw)]


def test_a_config_complete_is_not_captured_as_unknown(up):
    iface = type("I", (), {"_capturing": True, "_captured": [], "_config_ord": 0})()
    fr = mesh_pb2.FromRadio()
    fr.config_complete_id = 42
    up._on_raw_frame(fr.SerializeToString(), iface)
    assert iface._captured == []
