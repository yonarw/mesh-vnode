import time

import pytest

from mesh_vnode import protocol as proto
from mesh_vnode.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    yield database
    database.close()


def _store(db, *, packet_id, text="x", frm=1, origin=None, rx_time=None, portnum=1):
    return db.store_packet(
        raw=b"raw-" + str(packet_id).encode(),
        meta={
            "packet_id": packet_id,
            "from_num": frm,
            "to_num": proto.BROADCAST_NUM,
            "portnum": portnum,
            "rx_time": rx_time or int(time.time()),
            "text": text,
            "origin": origin,
        },
    )


def test_duplicate_packets_are_ignored(db):
    """The node re-delivers packets it heard more than once over the mesh."""
    assert _store(db, packet_id=7) == 1
    assert _store(db, packet_id=7) is None
    assert db.counts()["packets"] == 1


def test_same_id_from_different_nodes_both_stored(db):
    assert _store(db, packet_id=7, frm=1) is not None
    assert _store(db, packet_id=7, frm=2) is not None
    assert db.counts()["packets"] == 2


def test_cursor_returns_only_newer_packets(db):
    first = _store(db, packet_id=1)
    _store(db, packet_id=2)
    _store(db, packet_id=3)
    rows = db.packets_after(first, limit=10)
    assert [r["packet_id"] for r in rows] == [2, 3]


def test_offset_takes_the_newest_slice(db):
    for i in range(1, 11):
        _store(db, packet_id=i)
    backlog = db.count_after(0)
    assert backlog == 10
    rows = db.packets_after(0, limit=3, offset=backlog - 3)
    assert [r["packet_id"] for r in rows] == [8, 9, 10]


def test_replay_can_exclude_a_clients_own_messages(db):
    _store(db, packet_id=1, origin="192.0.2.5")
    _store(db, packet_id=2, origin=None)
    rows = db.packets_after(0, limit=10, exclude_origin="192.0.2.5")
    assert [r["packet_id"] for r in rows] == [2]


def test_portnum_filter_keeps_replay_to_text(db):
    _store(db, packet_id=1, portnum=proto.PORT_TEXT)
    _store(db, packet_id=2, portnum=proto.PORT_TELEMETRY)
    rows = db.packets_after(0, limit=10, portnums=proto.REPLAY_PORTNUMS)
    assert [r["packet_id"] for r in rows] == [1]


def test_client_cursor_only_moves_forward(db):
    db.get_or_create_client("10.0.0.5")
    db.set_client_cursor("10.0.0.5", 20)
    db.set_client_cursor("10.0.0.5", 5)
    assert db.clients()[0]["last_seq"] == 20
    db.reset_client_cursor("10.0.0.5")
    assert db.clients()[0]["last_seq"] == 0


def test_reconnect_counts_up_without_losing_the_cursor(db):
    db.get_or_create_client("10.0.0.5")
    db.set_client_cursor("10.0.0.5", 11)
    db.get_or_create_client("10.0.0.5")
    row = db.clients()[0]
    assert row["connects"] == 2
    assert row["last_seq"] == 11


def test_config_frames_replace_atomically(db):
    db.replace_config_frames([("my_info", "my_info", 0, b"a"), ("channel:0", "channel", 1, b"b")])
    db.replace_config_frames([("my_info", "my_info", 0, b"c")])
    frames = db.config_frames()
    assert [(f["key"], f["raw"]) for f in frames] == [("my_info", b"c")]


def test_config_frames_come_back_in_capture_order(db):
    db.replace_config_frames(
        [
            ("channel:0", "channel", 2, b"c"),
            ("my_info", "my_info", 0, b"a"),
            ("metadata", "metadata", 1, b"b"),
        ]
    )
    assert [f["raw"] for f in db.config_frames()] == [b"a", b"b", b"c"]


def test_prune_drops_old_packets_only(db):
    old = int(time.time()) - 40 * 86400
    _store(db, packet_id=1, rx_time=old)
    _store(db, packet_id=2)
    assert db.prune(30) == 1
    assert db.counts()["packets"] == 1


def test_telemetry_deduplicates_on_resend(db):
    db.store_telemetry(5, 1700000000, "device", {"battery_level": 80})
    db.store_telemetry(5, 1700000000, "device", {"battery_level": 80})
    assert db.counts()["telemetry"] == 1
