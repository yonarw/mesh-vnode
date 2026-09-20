"""What live traffic reaches the apps, and the node list they get on connect."""

import time

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import mesh_pb2, telemetry_pb2

from mesh_vnode import protocol as proto
from mesh_vnode.config import Settings
from mesh_vnode.web import create_app

from .test_vnode_server import MY_NUM, node_nums, running, seed_config, texts  # noqa: F401

PEER = 0x11AA22BB
STRANGER = 0x70000005


def packet(frm, port, payload=b"", *, to=proto.BROADCAST_NUM):
    pkt = mesh_pb2.MeshPacket(
        to=to, id=int(time.time() * 1000) & 0xFFFFFFF, rx_time=int(time.time())
    )
    pkt.__setattr__("from", frm)
    pkt.decoded.portnum = port
    pkt.decoded.payload = payload
    return pkt


def position(frm, lat=514769000):
    return packet(
        frm,
        proto.PORT_POSITION,
        mesh_pb2.Position(latitude_i=lat, longitude_i=-5000).SerializeToString(),
    )


def telemetry(frm):
    tel = telemetry_pb2.Telemetry()
    tel.device_metrics.battery_level = 77
    return packet(frm, proto.PORT_TELEMETRY, tel.SerializeToString())


def nodeinfo(frm, name, key=b""):
    user = mesh_pb2.User(id=proto.node_id(frm), long_name=name, short_name=name[:4], public_key=key)
    return packet(frm, proto.PORT_NODEINFO, user.SerializeToString())


def frame(pkt):
    return proto.parse_from_radio(proto.wrap_packet(pkt))


# ----------------------------------------------------------------- categories


def test_text_and_our_own_traffic_always_go_through():
    assert proto.forward_category(frame(packet(PEER, proto.PORT_TEXT, b"hi")), MY_NUM) is None
    ack = packet(PEER, proto.PORT_ROUTING, to=MY_NUM)
    assert proto.forward_category(frame(ack), MY_NUM) is None
    assert proto.forward_category(frame(position(MY_NUM)), MY_NUM) is None
    fr = mesh_pb2.FromRadio()
    fr.queueStatus.free = 3
    assert proto.forward_category(fr, MY_NUM) is None


def test_other_nodes_traffic_is_categorised():
    assert proto.forward_category(frame(position(PEER)), MY_NUM) == "position"
    assert proto.forward_category(frame(telemetry(PEER)), MY_NUM) == "telemetry"
    assert proto.forward_category(frame(nodeinfo(PEER, "Peer")), MY_NUM) == "nodeinfo"
    assert proto.forward_category(frame(packet(PEER, 71)), MY_NUM) == "other"  # neighbour info
    undecodable = mesh_pb2.FromRadio()
    undecodable.packet.encrypted = b"\x01\x02"
    assert proto.forward_category(undecodable, MY_NUM) == "other"


# ------------------------------------------------------------------ fan-out


class Favs:
    favorites = {PEER}


async def live(running, policy, pkts):  # noqa: F811
    server, _, upstream, connect = running
    server.forward_policy = policy
    upstream.favorites = {PEER}
    client = await connect()
    await client.want_config(1)
    await client.collect(0.4)
    for pkt in pkts:
        server._fanout(proto.wrap_packet(pkt), None, "packet")
    got = [
        f.packet for f in await client.collect(0.4) if f.WhichOneof("payload_variant") == "packet"
    ]
    await client.close()
    return [(p.__getattribute__("from"), p.decoded.portnum) for p in got]


async def test_everything_goes_through_by_default(running):  # noqa: F811
    got = await live(running, {}, [position(PEER), telemetry(STRANGER)])
    assert got == [(PEER, proto.PORT_POSITION), (STRANGER, proto.PORT_TELEMETRY)]


async def test_text_only(running):  # noqa: F811
    off = {c: "none" for c in proto.FORWARD_CATEGORIES}
    pkts = [
        position(PEER),
        telemetry(PEER),
        nodeinfo(PEER, "Peer"),
        packet(PEER, proto.PORT_TEXT, b"hi"),
    ]
    assert await live(running, off, pkts) == [(PEER, proto.PORT_TEXT)]


async def test_favourites_only_positions(running):  # noqa: F811
    policy = {"position": "favorites", "telemetry": "none", "nodeinfo": "all", "other": "all"}
    got = await live(running, policy, [position(PEER), position(STRANGER), telemetry(PEER)])
    assert got == [(PEER, proto.PORT_POSITION)]


async def test_answers_to_the_app_pass_any_filter(running):  # noqa: F811
    off = {c: "none" for c in proto.FORWARD_CATEGORIES}
    reply = packet(STRANGER, proto.PORT_POSITION, to=MY_NUM)  # answer to a position request
    assert await live(running, off, [reply]) == [(STRANGER, proto.PORT_POSITION)]


# ---------------------------------------------------------- fresh node list


def stored_node(db, num):
    rows = [r for r in db.config_frames(["node_info"]) if r["key"] == f"node_info:{num}"]
    return proto.parse_from_radio(rows[0]["raw"]).node_info if rows else None


async def test_a_position_updates_the_stored_node(running):  # noqa: F811
    _, db, _, _ = running
    db.refresh_node_frame(PEER, position(PEER, lat=515000000), 1000)
    info = stored_node(db, PEER)
    assert info.position.latitude_i == 515000000
    assert info.user.long_name == "Peer"  # untouched


async def test_telemetry_updates_battery_and_keeps_flags(running):  # noqa: F811
    _, db, _, _ = running
    db.refresh_node_frame(MY_NUM, telemetry(MY_NUM), 1000)
    info = stored_node(db, MY_NUM)
    assert info.device_metrics.battery_level == 77
    assert info.is_favorite  # set in the capture, kept


async def test_a_new_node_is_added_with_its_key(running):  # noqa: F811
    _, db, _, connect = running
    db.refresh_node_frame(STRANGER, position(STRANGER), 1000)
    assert stored_node(db, STRANGER) is None  # position alone: not yet
    db.refresh_node_frame(STRANGER, nodeinfo(STRANGER, "New one", key=bytes(32)), 1001)
    info = stored_node(db, STRANGER)
    assert (info.user.long_name, len(info.user.public_key)) == ("New one", 32)
    # And an app connecting now gets it, after the nodes it already knew.
    client = await connect()
    await client.want_config(proto.NONCE_ONLY_DB)
    assert node_nums(await client.collect(0.5)) == [MY_NUM, PEER, STRANGER]
    await client.close()


async def test_a_key_is_kept_when_an_announcement_leaves_it_out(running):  # noqa: F811
    _, db, _, _ = running
    db.refresh_node_frame(PEER, nodeinfo(PEER, "Peer", key=bytes(range(32))), 1000)
    db.refresh_node_frame(PEER, nodeinfo(PEER, "Peer renamed"), 1001)
    info = stored_node(db, PEER)
    assert info.user.long_name == "Peer renamed"
    assert bytes(info.user.public_key) == bytes(range(32))


def test_nothing_is_stored_before_the_first_capture(tmp_path):
    from mesh_vnode.db import Database

    db = Database(tmp_path / "e.sqlite3")
    db.refresh_node_frame(PEER, nodeinfo(PEER, "Peer"), 1000)
    assert db.config_frames() == []


# ---------------------------------------------------------------------- API


@pytest.fixture
def api(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "f.sqlite3"))
    return TestClient(app), app.state.vnode


def test_forwarding_defaults_to_everything(api):
    http, _ = api
    assert set(http.get("/api/prefs").json()["app_forward"].values()) == {"all"}


def test_forwarding_settings_apply_at_once(api):
    http, vnode = api
    body = {
        "app_forward": {
            "position": "favorites",
            "telemetry": "none",
            "nodeinfo": "all",
            "other": "none",
        }
    }
    assert http.put("/api/prefs", json=body).json()["app_forward"]["position"] == "favorites"
    assert vnode.server.forward_policy["telemetry"] == "none"


def test_node_info_has_no_favourites_mode(api):
    http, _ = api
    body = {"app_forward": {"nodeinfo": "favorites"}}
    assert http.put("/api/prefs", json=body).status_code == 422
