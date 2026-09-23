"""DMs from the web UI: sent and stored the way the Android app does it, and
shown to connected apps straight away."""

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import mesh_pb2

from mesh_vnode import protocol as proto
from mesh_vnode.config import Settings
from mesh_vnode.web import create_app

from .test_messages import FRIEND, ME, OTHER, seed_me, up  # noqa: F401

MY_KEY = bytes(range(32))
FRIEND_KEY = bytes(range(32, 64))


class FakeIface:
    myInfo = None  # noqa: N815 - the library's name; None makes it read the store

    def __init__(self):
        self.calls = []
        self.packets = []

    def sendData(self, data, **kw):  # noqa: N802 - the library's name
        self.calls.append((data, kw))
        pkt = mesh_pb2.MeshPacket(id=0x900 + len(self.calls), to=kw["destinationId"])
        pkt.decoded.portnum = kw["portNum"]
        pkt.decoded.payload = data
        pkt.pki_encrypted = bool(kw["pkiEncrypted"])
        return pkt

    # A reaction cannot go through sendData - it has no emoji parameter - so
    # send_text builds the packet and calls these two directly.
    def _generatePacketId(self):  # noqa: N802 - the library's name
        return 0xA00 + len(self.packets) + 1

    def _sendPacket(self, meshPacket, destinationId, **kw):  # noqa: N802,N803
        self.packets.append((meshPacket, destinationId, kw))
        meshPacket.to = destinationId if isinstance(destinationId, int) else proto.BROADCAST_NUM
        return meshPacket


@pytest.fixture
def keyed(up):  # noqa: F811
    up.db.upsert_node(ME, {"public_key": MY_KEY})
    up.db.upsert_node(FRIEND, {"public_key": FRIEND_KEY})
    up.iface = FakeIface()
    return up


def test_a_dm_is_sent_sealed_to_the_recipients_key(keyed):
    keyed.send_text("hi", destination=FRIEND)
    _, kw = keyed.iface.calls[0]
    assert kw["pkiEncrypted"] is True
    assert kw["publicKey"] == FRIEND_KEY


def test_a_dm_to_a_node_without_a_key_is_sent_plain(keyed):
    keyed.send_text("hi", destination=OTHER)
    _, kw = keyed.iface.calls[0]
    assert (kw["pkiEncrypted"], kw["publicKey"]) == (False, None)


def test_a_channel_message_is_never_sealed(keyed):
    keyed.send_text("hi")
    _, kw = keyed.iface.calls[0]
    assert kw["pkiEncrypted"] is False


def test_the_stored_copy_says_pki(keyed):
    pkt = keyed.send_text("hi", destination=FRIEND)
    keyed.store_client_packet(pkt, "webui")
    row = keyed.db.messages(limit=1)[0]
    assert row["pki"] == 1
    assert proto.parse_from_radio(row["raw"]).packet.pki_encrypted


def store_own(db, pid, *, to, origin, pki=False):
    pkt = mesh_pb2.MeshPacket(id=pid, to=to, pki_encrypted=pki)
    pkt.__setattr__("from", ME)
    pkt.decoded.portnum = proto.PORT_TEXT
    pkt.decoded.payload = b"x"
    meta = {**proto.packet_meta(pkt), "origin": origin}
    return db.store_packet(raw=proto.wrap_packet(pkt), meta=meta)


def test_earlier_web_dms_are_marked_pki_in_row_and_raw(keyed):
    db = keyed.db
    fixed = store_own(db, 0xA1, to=FRIEND, origin="webui")
    unknown = store_own(db, 0xA2, to=OTHER, origin="webui")  # no key known
    channel = store_own(db, 0xA3, to=proto.BROADCAST_NUM, origin="webui")
    phone = store_own(db, 0xA4, to=FRIEND, origin="192.0.2.5")  # the app's own say
    assert db.seal_own_dms(ME, "webui") == 1
    pki = {r["seq"]: r["pki"] for r in db.messages(limit=10)}
    assert pki == {fixed: 1, unknown: 0, channel: 0, phone: 0}
    raw = db.packet(fixed)["raw"]
    assert proto.parse_from_radio(raw).packet.pki_encrypted
    assert db.seal_own_dms(ME, "webui") == 0  # idempotent


def test_a_nodeinfo_packet_brings_the_public_key():
    pkt = mesh_pb2.MeshPacket()
    pkt.__setattr__("from", FRIEND)
    pkt.decoded.portnum = proto.PORT_NODEINFO
    pkt.decoded.payload = mesh_pb2.User(id="!11aa22bb", public_key=FRIEND_KEY).SerializeToString()
    assert proto.decode_nodeinfo(pkt)["public_key"] == FRIEND_KEY


# ------------------------------------------------------------------ mirror


def test_a_web_send_is_pushed_to_connected_apps(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "m.sqlite3"))
    vnode = app.state.vnode
    seed_me(vnode.db)
    vnode.upstream.connected.set()
    vnode.upstream.iface = FakeIface()
    mirrored = []
    vnode.server.mirror_outgoing = lambda pkt, seq, exclude=None: mirrored.append((pkt.id, seq))
    res = TestClient(app).post("/api/send", json={"text": "hi", "to": FRIEND})
    assert res.status_code == 200
    assert mirrored == [(res.json()["packet_id"], res.json()["seq"])]


def test_the_node_list_survives_binary_keys(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "n.sqlite3"))
    app.state.vnode.db.upsert_node(FRIEND, {"short_name": "RR", "public_key": b"\xff\xfe" * 16})
    [node] = TestClient(app).get("/api/nodes").json()
    assert node["has_public_key"] is True
    assert "public_key" not in node


# ---------------------------------------------------------------- reactions


def test_a_reaction_goes_out_as_a_text_packet_with_emoji_set(keyed):
    """The wire format of a tapback: the payload is the emoji, `emoji` marks it
    as a reaction rather than a one-character message, and `reply_id` says which
    message it belongs to."""
    keyed.send_text("\U0001f44d", destination=FRIEND, reply_id=0x123, emoji=True)
    packet, destination, kw = keyed.iface.packets[0]
    assert packet.decoded.emoji == 1
    assert packet.decoded.reply_id == 0x123
    assert packet.decoded.payload.decode() == "\U0001f44d"
    assert packet.decoded.portnum == proto.PORT_TEXT
    assert destination == FRIEND
    # Sealed like any other DM to a node whose key we hold.
    assert kw["publicKey"] == FRIEND_KEY


def test_an_ordinary_message_still_goes_through_senddata(keyed):
    keyed.send_text("hi", destination=FRIEND)
    assert keyed.iface.calls and not keyed.iface.packets


def test_a_reaction_is_folded_into_the_message_it_targets(tmp_path):
    """End to end: the reaction is stored like any packet, and /api/messages
    hangs it off its target instead of listing it as a message of its own."""
    app = create_app(Settings(db_path=tmp_path / "r.sqlite3"))
    vnode = app.state.vnode
    seed_me(vnode.db)
    vnode.upstream.connected.set()
    vnode.upstream.iface = FakeIface()
    http = TestClient(app)

    sent = http.post("/api/send", json={"text": "hi", "channel": 0}).json()
    res = http.post(
        "/api/send",
        json={"text": "\U0001f44d", "channel": 0, "emoji": True, "reply_id": sent["packet_id"]},
    )
    assert res.status_code == 200

    messages = http.get("/api/messages?channel=0").json()
    assert [m["text"] for m in messages] == ["hi"]
    [target] = [m for m in messages if m["packet_id"] == sent["packet_id"]]
    assert [r["emoji"] for r in target["reactions"]] == ["\U0001f44d"]
