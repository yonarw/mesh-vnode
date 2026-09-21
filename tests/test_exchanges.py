"""Asking a node for something: traceroutes and position/telemetry/node info.

The point of these is the matching. A request goes out and nothing waits for
it; whatever comes back from the mesh minutes later has to find its way to the
row that asked, or be recognised as a question somebody asked of us.
"""

import time

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import mesh_pb2, telemetry_pb2

from mesh_vnode import protocol as proto
from mesh_vnode.config import Settings
from mesh_vnode.db import Database
from mesh_vnode.upstream import Upstream
from mesh_vnode.web import create_app

ME = 0x433A1B2C
FRIEND = 0x11AA22BB
RELAY = 0x77CC88DD


class FakeIface:
    """Records what would have gone on air, the way sendData builds it."""

    myInfo = None  # our node number comes from the captured handshake instead

    def __init__(self) -> None:
        self.sent: list[mesh_pb2.MeshPacket] = []
        self.next_id = 0x1000

    def sendData(self, data, *, destinationId, portNum, wantResponse, channelIndex, hopLimit):
        pkt = mesh_pb2.MeshPacket()
        pkt.__setattr__("from", ME)
        pkt.to = destinationId
        pkt.id = self.next_id
        self.next_id += 1
        pkt.channel = channelIndex
        pkt.hop_limit = hopLimit or 3
        pkt.decoded.portnum = portNum
        pkt.decoded.payload = data
        pkt.decoded.want_response = wantResponse
        self.sent.append(pkt)
        return pkt


def seed_me(db: Database) -> None:
    fr = mesh_pb2.FromRadio()
    fr.my_info.my_node_num = ME
    db.replace_config_frames([("my_info", "my_info", 0, fr.SerializeToString())])
    db.upsert_node(ME, {"node_id": proto.node_id(ME), "long_name": "Virtual", "short_name": "VN"})


@pytest.fixture
def up(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    seed_me(db)
    upstream = Upstream(Settings(db_path=tmp_path / "t.sqlite3"), db)
    upstream.events = []
    upstream.add_event_sink(lambda e, p: upstream.events.append((e, p)))
    upstream.iface = FakeIface()
    yield upstream
    db.close()


def store(up, pkt):
    fr = mesh_pb2.FromRadio()
    fr.packet.CopyFrom(pkt)
    return up._store_packet(pkt, fr.SerializeToString())


def answer(frm, request_id, portnum, payload, *, to=ME, hop_start=0, hop_limit=0):
    pkt = mesh_pb2.MeshPacket()
    pkt.__setattr__("from", frm)
    pkt.to = to
    pkt.id = request_id ^ 0x9999
    pkt.rx_time = int(time.time())
    pkt.hop_start = hop_start
    pkt.hop_limit = hop_limit
    pkt.decoded.portnum = portnum
    pkt.decoded.payload = payload
    pkt.decoded.request_id = request_id
    return pkt


def route_payload(route, snr_towards, route_back=(), snr_back=()):
    rd = mesh_pb2.RouteDiscovery()
    rd.route.extend(route)
    rd.snr_towards.extend(snr_towards)
    rd.route_back.extend(route_back)
    rd.snr_back.extend(snr_back)
    return rd.SerializeToString()


def routing_packet(frm, request_id, error):
    pkt = mesh_pb2.MeshPacket()
    pkt.__setattr__("from", frm)
    pkt.to = ME
    pkt.id = request_id ^ 0x5555
    pkt.decoded.portnum = proto.PORT_ROUTING
    pkt.decoded.request_id = request_id
    pkt.decoded.payload = mesh_pb2.Routing(error_reason=error).SerializeToString()
    return pkt


# ------------------------------------------------------------------ sending


def test_a_request_goes_out_on_the_chosen_channel_asking_for_an_answer(up):
    up.send_request("traceroute", FRIEND, channel_index=2)
    pkt = up.iface.sent[0]
    assert pkt.to == FRIEND
    assert pkt.channel == 2
    assert pkt.decoded.portnum == proto.PORT_TRACEROUTE
    assert pkt.decoded.want_response is True


def test_a_request_is_stored_as_open_with_its_packet_id(up):
    row = up.send_request("position", FRIEND)
    assert (row["kind"], row["direction"], row["status"]) == ("position", "out", "sent")
    assert row["packet_id"] == up.iface.sent[0].id
    assert row["origin"] == "webui"
    assert up.events[-1][0] == "exchange"


def test_a_position_request_carries_our_own_position(up):
    up.db.upsert_node(ME, {"latitude": 52.5, "longitude": 13.4})
    up.send_request("position", FRIEND)
    pos = mesh_pb2.Position()
    pos.ParseFromString(up.iface.sent[0].decoded.payload)
    assert round(pos.latitude_i * 1e-7, 4) == 52.5


def test_a_nodeinfo_request_carries_our_own_user(up):
    up.send_request("nodeinfo", FRIEND)
    user = mesh_pb2.User()
    user.ParseFromString(up.iface.sent[0].decoded.payload)
    assert (user.long_name, user.short_name) == ("Virtual", "VN")


def test_the_hop_limit_only_stretches_as_far_as_the_node(up):
    up.db.upsert_node(FRIEND, {"hops_away": 1})
    up.send_request("traceroute", FRIEND)
    assert up.iface.sent[0].hop_limit == 2


def test_an_unknown_kind_is_refused(up):
    with pytest.raises(ValueError):
        up.send_request("reboot", FRIEND)


# ----------------------------------------------------------------- answers


def test_a_traceroute_answer_closes_the_request_with_the_route(up):
    row = up.send_request("traceroute", FRIEND)
    store(
        up,
        answer(
            FRIEND,
            row["packet_id"],
            proto.PORT_TRACEROUTE,
            route_payload([RELAY], [20, -128], [RELAY], [16, 8]),
            hop_start=3,
            hop_limit=1,
        ),
    )
    done = up.db.exchange(row["id"])
    assert done["status"] == "answered"
    result = up.db.exchanges(FRIEND)[0]
    assert done["response_ts"] is not None
    import json

    parsed = json.loads(result["result"])
    assert parsed["route"] == [RELAY]
    # 1/4 dB units, and -128 means the node did not know.
    assert parsed["snr_towards"] == [5.0, None]
    assert parsed["snr_back"] == [4.0, 2.0]
    assert parsed["hops"] == 2


def test_a_telemetry_answer_is_kept_with_the_request(up):
    row = up.send_request("telemetry", FRIEND)
    tel = telemetry_pb2.Telemetry()
    tel.device_metrics.battery_level = 78
    tel.device_metrics.voltage = 3.9
    store(up, answer(FRIEND, row["packet_id"], proto.PORT_TELEMETRY, tel.SerializeToString()))
    import json

    result = json.loads(up.db.exchange(row["id"])["result"])
    assert result["metric"] == "device"
    assert result["battery_level"] == 78


def test_an_answer_still_updates_the_node_tables(up):
    """The exchange row is a record of the asking; the data itself lands where
    the map and the node list already look for it."""
    row = up.send_request("position", FRIEND)
    pos = mesh_pb2.Position(latitude_i=int(48.1 / 1e-7), longitude_i=int(11.6 / 1e-7))
    store(up, answer(FRIEND, row["packet_id"], proto.PORT_POSITION, pos.SerializeToString()))
    assert round(up.db.node(FRIEND)["latitude"], 3) == 48.1


def test_an_answer_to_a_request_we_never_made_is_ignored(up):
    store(up, answer(FRIEND, 0xDEAD, proto.PORT_TRACEROUTE, route_payload([], [])))
    assert up.db.exchanges() == []


def test_a_routing_error_fails_the_request(up):
    row = up.send_request("traceroute", FRIEND)
    store(up, routing_packet(ME, row["packet_id"], mesh_pb2.Routing.Error.NO_RESPONSE))
    done = up.db.exchange(row["id"])
    assert (done["status"], done["error"]) == ("failed", "NO_RESPONSE")


def test_a_plain_ack_leaves_the_request_open(up):
    """The question reached the mesh; the answer is still on its way."""
    row = up.send_request("traceroute", FRIEND)
    store(up, routing_packet(ME, row["packet_id"], mesh_pb2.Routing.Error.NONE))
    assert up.db.exchange(row["id"])["status"] == "sent"
    assert up.db.delivery_log(row["packet_id"], ME) == []


def test_an_answer_after_a_failure_still_counts(up):
    row = up.send_request("traceroute", FRIEND)
    store(up, routing_packet(ME, row["packet_id"], mesh_pb2.Routing.Error.NO_RESPONSE))
    store(up, answer(FRIEND, row["packet_id"], proto.PORT_TRACEROUTE, route_payload([], [20])))
    assert up.db.exchange(row["id"])["status"] == "answered"


def test_requests_that_nothing_answered_are_written_off(up):
    row = up.send_request("telemetry", FRIEND)
    up.db._exec("UPDATE exchanges SET ts = ts - 600 WHERE id = ?", (row["id"],))
    up.db.expire_exchanges({"traceroute": 180}, 90)
    assert up.db.exchange(row["id"])["status"] == "expired"


def test_a_traceroute_gets_longer_before_it_is_written_off(up):
    row = up.send_request("traceroute", FRIEND)
    up.db._exec("UPDATE exchanges SET ts = ts - 120 WHERE id = ?", (row["id"],))
    up.db.expire_exchanges({"traceroute": 180}, 90)
    assert up.db.exchange(row["id"])["status"] == "sent"


# --------------------------------------------------------- what others ask


def test_a_question_another_node_asks_of_us_is_recorded(up):
    pkt = mesh_pb2.MeshPacket()
    pkt.__setattr__("from", FRIEND)
    pkt.to = ME
    pkt.id = 0x4242
    pkt.rx_time = int(time.time())
    pkt.decoded.portnum = proto.PORT_POSITION
    pkt.decoded.want_response = True
    store(up, pkt)
    row = up.db.exchanges(FRIEND)[0]
    assert (row["kind"], row["direction"], row["status"]) == ("position", "in", "heard")


def test_a_plain_broadcast_is_not_a_question(up):
    pkt = mesh_pb2.MeshPacket()
    pkt.__setattr__("from", FRIEND)
    pkt.to = proto.BROADCAST_NUM
    pkt.id = 0x4243
    pkt.decoded.portnum = proto.PORT_POSITION
    pkt.decoded.payload = mesh_pb2.Position(latitude_i=1, longitude_i=1).SerializeToString()
    store(up, pkt)
    assert up.db.exchanges() == []


def test_a_question_asked_of_somebody_else_is_not_ours(up):
    pkt = mesh_pb2.MeshPacket()
    pkt.__setattr__("from", FRIEND)
    pkt.to = RELAY
    pkt.id = 0x4244
    pkt.decoded.portnum = proto.PORT_TRACEROUTE
    pkt.decoded.want_response = True
    store(up, pkt)
    assert up.db.exchanges() == []


# --------------------------------------------------------------------- API


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "api.sqlite3"))
    vnode = app.state.vnode
    seed_me(vnode.db)
    vnode.db.upsert_node(FRIEND, {"short_name": "RR", "long_name": "Hilltop Relay"})
    vnode.upstream.iface = FakeIface()
    vnode.upstream.connected.set()
    # No `with`: the lifespan would try to reach a real node.
    return TestClient(app), vnode


def test_the_api_sends_the_request_and_returns_the_row(client):
    http, vnode = client
    row = http.post("/api/exchange", json={"kind": "traceroute", "node": FRIEND}).json()
    assert row["status"] == "sent"
    assert row["node"] == {"short": "RR", "long": "Hilltop Relay", "id": "!11aa22bb"}
    assert vnode.upstream.iface.sent[0].to == FRIEND


def test_the_api_passes_the_chosen_channel_on(client):
    http, vnode = client
    http.post("/api/exchange", json={"kind": "position", "node": FRIEND, "channel": 3})
    assert vnode.upstream.iface.sent[0].channel == 3


def test_asking_the_same_thing_twice_in_a_row_is_refused(client):
    """The firmware rate-limits these and they cost everyone in range airtime."""
    http, _ = client
    assert http.post("/api/exchange", json={"kind": "position", "node": FRIEND}).status_code == 200
    again = http.post("/api/exchange", json={"kind": "position", "node": FRIEND})
    assert again.status_code == 429
    # A different question about the same node is not the same request.
    assert http.post("/api/exchange", json={"kind": "nodeinfo", "node": FRIEND}).status_code == 200


def test_the_api_rejects_a_kind_the_node_cannot_be_asked(client):
    http, _ = client
    assert http.post("/api/exchange", json={"kind": "reboot", "node": FRIEND}).status_code == 422


def test_asking_this_node_about_itself_is_refused(client):
    http, _ = client
    assert http.post("/api/exchange", json={"kind": "position", "node": ME}).status_code == 400


def test_the_api_refuses_while_the_node_is_unreachable(client):
    http, vnode = client
    vnode.upstream.connected.clear()
    assert http.post("/api/exchange", json={"kind": "position", "node": FRIEND}).status_code == 503


def test_listed_traceroutes_name_the_nodes_on_the_route(client):
    http, vnode = client
    row = http.post("/api/exchange", json={"kind": "traceroute", "node": FRIEND}).json()
    vnode.upstream._store_packet(
        answer(FRIEND, row["packet_id"], proto.PORT_TRACEROUTE, route_payload([FRIEND], [20])),
        b"",
    )
    listed = http.get(f"/api/exchanges?node={FRIEND}").json()[0]
    assert listed["status"] == "answered"
    assert listed["result"]["route_names"] == [
        {"short": "RR", "long": "Hilltop Relay", "id": "!11aa22bb"}
    ]
