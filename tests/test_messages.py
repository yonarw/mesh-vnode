"""Reactions, sender names and delivery status - what the chat view shows."""

import time

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import mesh_pb2

from mesh_vnode import protocol as proto
from mesh_vnode.config import Settings
from mesh_vnode.db import Database
from mesh_vnode.upstream import Upstream
from mesh_vnode.web import SendRequest, create_app

ME = 0x433A1B2C
FRIEND = 0x11AA22BB
OTHER = 0x55EE66FF


def text_packet(frm, pid, text, *, to=proto.BROADCAST_NUM, reply_id=0, emoji=0):
    pkt = mesh_pb2.MeshPacket()
    pkt.__setattr__("from", frm)
    pkt.to = to
    pkt.id = pid
    pkt.rx_time = int(time.time())
    pkt.decoded.portnum = proto.PORT_TEXT
    pkt.decoded.payload = text.encode()
    pkt.decoded.reply_id = reply_id
    pkt.decoded.emoji = emoji
    return pkt


def routing_packet(frm, request_id, error=mesh_pb2.Routing.Error.NONE):
    pkt = mesh_pb2.MeshPacket()
    pkt.__setattr__("from", frm)
    pkt.to = ME
    pkt.id = request_id ^ 0x5555
    pkt.decoded.portnum = proto.PORT_ROUTING
    pkt.decoded.request_id = request_id
    pkt.decoded.payload = mesh_pb2.Routing(error_reason=error).SerializeToString()
    return pkt


def seed_me(db: Database) -> None:
    fr = mesh_pb2.FromRadio()
    fr.my_info.my_node_num = ME
    db.replace_config_frames([("my_info", "my_info", 0, fr.SerializeToString())])


@pytest.fixture
def up(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    seed_me(db)
    upstream = Upstream(Settings(db_path=tmp_path / "t.sqlite3"), db)
    upstream.events = []
    upstream.add_event_sink(lambda e, p: upstream.events.append((e, p)))
    yield upstream
    db.close()


def store(up, pkt):
    fr = mesh_pb2.FromRadio()
    fr.packet.CopyFrom(pkt)
    return up._store_packet(pkt, fr.SerializeToString())


def send(up, pid, *, to=FRIEND):
    return up.store_client_packet(text_packet(0, pid, "hi", to=to), "webui")


def status_of(up, pid):
    return up.db.find_own_packet(pid, ME)["status"]


# ----------------------------------------------------------------- status


def test_a_sent_message_starts_pending(up):
    send(up, 0x100)
    assert status_of(up, 0x100) == "pending"


def test_queue_status_means_sent(up):
    send(up, 0x101)
    fr = mesh_pb2.FromRadio()
    fr.queueStatus.mesh_packet_id = 0x101
    up._on_raw_frame(fr.SerializeToString(), type("I", (), {"_capturing": False})())
    assert status_of(up, 0x101) == "sent"


def test_our_own_nodes_ack_means_relayed(up):
    """The implicit ack: our node heard someone repeat the message."""
    send(up, 0x102)
    store(up, routing_packet(ME, 0x102))
    assert status_of(up, 0x102) == "relayed"


def test_an_ack_from_the_recipient_means_delivered(up):
    send(up, 0x103)
    store(up, routing_packet(FRIEND, 0x103))
    assert status_of(up, 0x103) == "delivered"


def test_an_ack_with_from_zero_is_not_mistaken_for_the_recipient(up):
    send(up, 0x104)
    store(up, routing_packet(0, 0x104))
    assert status_of(up, 0x104) == "relayed"


def test_a_nak_means_failed_with_the_reason(up):
    send(up, 0x105)
    store(up, routing_packet(ME, 0x105, mesh_pb2.Routing.Error.MAX_RETRANSMIT))
    row = up.db.find_own_packet(0x105, ME)
    assert row["status"] == "failed"
    assert up.db.messages(limit=1)[0]["status_detail"] == "MAX_RETRANSMIT"


def test_status_never_goes_backwards(up):
    send(up, 0x106)
    store(up, routing_packet(FRIEND, 0x106))
    fr = mesh_pb2.FromRadio()
    fr.queueStatus.mesh_packet_id = 0x106
    up._on_raw_frame(fr.SerializeToString(), type("I", (), {"_capturing": False})())
    store(up, routing_packet(ME, 0x106, mesh_pb2.Routing.Error.MAX_RETRANSMIT))
    assert status_of(up, 0x106) == "delivered"


def test_a_late_ack_rescues_a_failed_message(up):
    send(up, 0x107)
    store(up, routing_packet(ME, 0x107, mesh_pb2.Routing.Error.NO_RESPONSE))
    store(up, routing_packet(FRIEND, 0x107))
    assert status_of(up, 0x107) == "delivered"


def test_a_status_that_beats_the_insert_is_applied_afterwards(up):
    """The web UI sends through the library, which transmits before we store."""
    fr = mesh_pb2.FromRadio()
    fr.queueStatus.mesh_packet_id = 0x108
    up._on_raw_frame(fr.SerializeToString(), type("I", (), {"_capturing": False})())
    send(up, 0x108)
    assert status_of(up, 0x108) == "sent"


def test_status_changes_are_announced(up):
    send(up, 0x109)
    store(up, routing_packet(FRIEND, 0x109))
    assert (
        "status",
        {"seq": 1, "packet_id": 0x109, "status": "delivered", "detail": None},
    ) in up.events


def test_acks_for_other_peoples_packets_change_nothing(up):
    store(up, text_packet(OTHER, 0x200, "not ours"))
    store(up, routing_packet(FRIEND, 0x200))
    assert up.db.messages(limit=1)[0]["status"] is None


def test_received_messages_have_no_status(up):
    store(up, text_packet(FRIEND, 0x201, "hello"))
    assert up.db.messages(limit=1)[0]["status"] is None


# -------------------------------------------------------------- reactions


def test_reaction_fields_are_stored(up):
    store(up, text_packet(FRIEND, 0x300, "👍", reply_id=0x0000A001, emoji=1))
    row = up.db.messages(limit=1)[0]
    assert row["emoji"] == 1
    assert row["reply_id"] == 0x0000A001


def test_old_rows_are_backfilled_from_raw_bytes(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = Database(path)
    pkt = text_packet(FRIEND, 0x301, "👍", reply_id=0x0000A001, emoji=1)
    fr = mesh_pb2.FromRadio()
    fr.packet.CopyFrom(pkt)
    db.store_packet(raw=fr.SerializeToString(), meta={**proto.packet_meta(pkt)})
    # Simulate a row written before the columns existed.
    db._conn.execute("UPDATE packets SET reply_id = NULL, emoji = 0")
    db._conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    db._conn.commit()
    db.close()

    db = Database(path)
    row = db.messages(limit=1)[0]
    assert (row["emoji"], row["reply_id"]) == (1, 0x0000A001)
    db.close()


# -------------------------------------------------------------------- API


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "api.sqlite3"))
    db = app.state.vnode.db
    seed_me(db)
    db.upsert_node(FRIEND, {"short_name": "RR", "long_name": "Hilltop Relay"})
    # No `with`: the lifespan would try to reach a real node.
    return TestClient(app), db


def put(db, pkt, origin=None):
    fr = mesh_pb2.FromRadio()
    fr.packet.CopyFrom(pkt)
    return db.store_packet(
        raw=fr.SerializeToString(), meta={**proto.packet_meta(pkt), "origin": origin}
    )


def test_messages_carry_the_senders_short_name(client):
    http, db = client
    put(db, text_packet(FRIEND, 0x400, "hello"))
    msg = http.get("/api/messages").json()[0]
    assert msg["sender"] == {"short": "RR", "long": "Hilltop Relay", "id": "!11aa22bb"}


def test_unknown_senders_fall_back_to_the_id(client):
    http, db = client
    put(db, text_packet(OTHER, 0x401, "who"))
    assert http.get("/api/messages").json()[0]["sender"]["short"] is None


def test_a_reaction_is_attached_to_its_message_not_listed(client):
    """The real case: a thumbs-up on a broadcast sent from the web UI."""
    http, db = client
    put(db, text_packet(ME, 0x0000A001, "test via webui"), origin="webui")
    put(db, text_packet(FRIEND, 0x0000A002, "👍", reply_id=0x0000A001, emoji=1))

    msgs = http.get("/api/messages").json()
    assert [m["text"] for m in msgs] == ["test via webui"]
    assert msgs[0]["reactions"] == [
        {
            "emoji": "👍",
            "from_num": FRIEND,
            "sender": {"short": "RR", "long": "Hilltop Relay", "id": "!11aa22bb"},
        }
    ]


def test_a_reaction_to_a_message_not_on_the_page_is_kept_and_flagged(client):
    http, db = client
    put(db, text_packet(OTHER, 0x0000A003, "3️⃣", reply_id=0x0000A004, emoji=1))
    msgs = http.get("/api/messages").json()
    assert len(msgs) == 1
    assert msgs[0]["is_reaction"] is True


def test_a_reply_is_still_a_message(client):
    """reply_id without emoji is a threaded reply, not a reaction."""
    http, db = client
    put(db, text_packet(ME, 0x500, "question"), origin="webui")
    put(db, text_packet(FRIEND, 0x501, "answer", reply_id=0x500))
    assert [m["text"] for m in http.get("/api/messages").json()] == ["question", "answer"]


def test_reactions_do_not_count_as_the_last_message_of_a_chat(client):
    http, db = client
    put(db, text_packet(FRIEND, 0x600, "real message", to=ME))
    put(db, text_packet(FRIEND, 0x601, "👍", to=ME, reply_id=0x600, emoji=1))
    direct = http.get("/api/conversations").json()["direct"]
    assert direct[0]["last_text"] == "real message"
    assert direct[0]["count"] == 1


def test_web_sends_ask_for_an_ack_by_default():
    assert SendRequest(text="hi").want_ack is True


def test_the_text_limit_counts_bytes_not_characters():
    SendRequest(text="a" * 228)
    with pytest.raises(ValueError):
        SendRequest(text="ä" * 115)
