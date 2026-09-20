"""Web-UI settings, channel names, the delivery log and upstream recovery."""

import threading

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import config_pb2, mesh_pb2

from mesh_vnode import protocol as proto
from mesh_vnode.config import Settings
from mesh_vnode.upstream import _Interface
from mesh_vnode.web import create_app, map_choice

from .test_messages import FRIEND, ME, routing_packet, seed_me, send, store, up  # noqa: F401

# ------------------------------------------------------------ channel names


def lora(preset=0, use_preset=True):
    return config_pb2.Config.LoRaConfig(modem_preset=preset, use_preset=use_preset)


def test_a_named_channel_keeps_its_name():
    assert proto.channel_name("hikers", lora()) == "hikers"


def test_an_unnamed_channel_is_called_after_the_preset():
    assert proto.channel_name("", lora(0)) == "LongFast"
    assert proto.channel_name("", lora(4)) == "MediumFast"


def test_presets_newer_than_the_python_package_are_named_too():
    assert proto.channel_name("", lora(16)) == "MediumTurbo"


def test_the_firmware_abbreviates_long_moderate():
    assert proto.channel_name("", lora(7)) == "LongMod"


def test_an_unnamed_channel_off_preset_is_custom():
    assert proto.channel_name("", lora(0, use_preset=False)) == "Custom"


# ------------------------------------------------------------- delivery log


def events(up, pid):  # noqa: F811 - `up` is the fixture
    return [dict(r) for r in up.db.delivery_log(pid, ME)]


def test_the_implicit_ack_records_who_repeated_it(up):  # noqa: F811
    send(up, 0x700, to=proto.BROADCAST_NUM)
    ack = routing_packet(ME, 0x700)
    ack.relay_node = FRIEND & 0xFF
    ack.rx_snr = 6.5
    ack.rx_rssi = -92
    store(up, ack)
    [entry] = events(up, 0x700)
    assert entry["event"] == "implicit_ack"
    assert entry["relay_node"] == FRIEND & 0xFF
    assert (entry["rx_snr"], entry["rx_rssi"]) == (6.5, -92)


def test_a_recipients_ack_is_logged_with_its_sender(up):  # noqa: F811
    send(up, 0x701)
    store(up, routing_packet(FRIEND, 0x701))
    [entry] = events(up, 0x701)
    assert (entry["event"], entry["ack_from"]) == ("ack", FRIEND)


def test_evidence_is_logged_even_when_the_status_does_not_move(up):  # noqa: F811
    send(up, 0x702)
    store(up, routing_packet(FRIEND, 0x702))
    store(up, routing_packet(ME, 0x702))  # a late implicit ack after delivery
    assert [e["event"] for e in events(up, 0x702)] == ["ack", "implicit_ack"]
    assert up.db.find_own_packet(0x702, ME)["status"] == "delivered"


def test_a_nak_is_logged_with_its_reason(up):  # noqa: F811
    send(up, 0x703)
    store(up, routing_packet(ME, 0x703, mesh_pb2.Routing.Error.MAX_RETRANSMIT))
    [entry] = events(up, 0x703)
    assert (entry["event"], entry["error"]) == ("nak", "MAX_RETRANSMIT")


def test_evidence_that_beats_the_insert_is_logged_afterwards(up):  # noqa: F811
    store(up, routing_packet(ME, 0x704))
    assert events(up, 0x704) == []
    send(up, 0x704)
    assert [e["event"] for e in events(up, 0x704)] == ["implicit_ack"]


def test_acks_for_packets_that_are_not_ours_are_not_logged(up):  # noqa: F811
    store(up, routing_packet(FRIEND, 0x705))
    assert up.db._query("SELECT COUNT(*) AS n FROM delivery_log")[0]["n"] == 0


# ---------------------------------------------------------------------- API


@pytest.fixture
def api(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "api.sqlite3", upstream_host="192.0.2.10"))
    vnode = app.state.vnode
    seed_me(vnode.db)
    vnode.db.upsert_node(ME, {"short_name": "HOME", "long_name": "Home Base"})
    vnode.db.upsert_node(FRIEND, {"short_name": "RR", "long_name": "Hilltop Relay", "hops_away": 0})
    # No `with`: the lifespan would try to reach a real node.
    return TestClient(app), vnode


def test_prefs_start_from_the_environment(api):
    http, _ = api
    prefs = http.get("/api/prefs").json()
    assert prefs["upstream_host"] == "192.0.2.10"
    assert prefs["upstream_source"] == "environment"
    assert prefs["muted"] == []


def test_a_new_node_address_is_stored_and_used(api):
    http, vnode = api
    prefs = http.put("/api/prefs", json={"upstream_host": "192.0.2.20"}).json()
    assert prefs["upstream_host"] == "192.0.2.20"
    assert prefs["upstream_source"] == "web ui"
    assert vnode.settings.upstream_host == "192.0.2.20"
    assert vnode.upstream._kick.is_set()


def test_the_default_basemap_needs_no_key():
    assert map_choice({}) == ("openfreemap", "dark")


def test_a_database_from_before_the_provider_setting_stays_on_carto():
    # Only a CARTO style was stored, or only a key: either way the map the user
    # already had must not change under them on upgrade.
    assert map_choice({"map_style": "dark-matter"}) == ("carto", "dark-matter")
    assert map_choice({"carto_api_key": "abc"}) == ("carto", "dark-matter")


def test_a_style_the_provider_does_not_have_falls_back_to_its_first():
    assert map_choice({"map_provider": "openfreemap", "map_style": "voyager"}) == (
        "openfreemap",
        "dark",
    )


def test_a_style_both_providers_have_is_kept():
    assert map_choice({"map_provider": "openfreemap", "map_style": "positron"}) == (
        "openfreemap",
        "positron",
    )


def test_switching_provider_is_stored(api):
    http, _ = api
    prefs = http.put("/api/prefs", json={"map_provider": "carto", "map_style": "voyager"}).json()
    assert (prefs["map_provider"], prefs["map_style"]) == ("carto", "voyager")
    assert http.put("/api/prefs", json={"map_provider": "nowhere"}).status_code == 422


def test_resetting_the_address_falls_back_to_the_environment(api):
    http, vnode = api
    http.put("/api/prefs", json={"upstream_host": "192.0.2.20"})
    http.put("/api/prefs", json={"upstream_host": None})
    assert vnode.settings.upstream_host == "192.0.2.10"


def test_the_stored_address_survives_a_restart(tmp_path):
    settings = Settings(db_path=tmp_path / "r.sqlite3", upstream_host="192.0.2.10")
    TestClient(create_app(settings)).put("/api/prefs", json={"upstream_host": "192.0.2.30"})
    fresh = create_app(Settings(db_path=tmp_path / "r.sqlite3", upstream_host="192.0.2.10"))
    assert fresh.state.vnode.settings.upstream_host == "192.0.2.30"


def test_the_command_line_wins_over_the_web_ui(tmp_path):
    settings = Settings(db_path=tmp_path / "c.sqlite3", upstream_host="192.0.2.10")
    TestClient(create_app(settings)).put("/api/prefs", json={"upstream_host": "192.0.2.30"})
    cli = Settings(db_path=tmp_path / "c.sqlite3", upstream_host="192.0.2.40")
    app = create_app(cli, cli_upstream=True)
    assert app.state.vnode.settings.upstream_host == "192.0.2.40"
    res = TestClient(app).put("/api/prefs", json={"upstream_host": "192.0.2.50"})
    assert res.status_code == 409
    assert app.state.vnode.db.prefs()["upstream_host"] == "192.0.2.30"


def test_ui_prefs_round_trip(api):
    http, _ = api
    body = {
        "carto_api_key": " abc123 ",
        "map_style": "positron",
        "muted": ["ch:0", f"dm:{FRIEND}"],
        "telemetry_display": {"noise": "graph", "battery": "number"},
    }
    prefs = http.put("/api/prefs", json=body).json()
    assert prefs["carto_api_key"] == "abc123"
    assert prefs["muted"] == ["ch:0", f"dm:{FRIEND}"]
    assert prefs["telemetry_display"]["battery"] == "number"


def test_prefs_reject_unknown_values(api):
    http, _ = api
    assert http.put("/api/prefs", json={"map_style": "neon"}).status_code == 422
    bad = {"telemetry_display": {"noise": "pie"}}
    assert http.put("/api/prefs", json=bad).status_code == 422


def test_the_primary_channel_is_named_after_the_preset(api):
    http, vnode = api
    ch = mesh_pb2.FromRadio()
    ch.channel.index = 0
    ch.channel.role = 1  # PRIMARY
    cfg = mesh_pb2.FromRadio()
    cfg.config.lora.use_preset = True
    cfg.config.lora.modem_preset = 4
    frames = vnode.db.config_frames()
    vnode.db.replace_config_frames(
        [(f["key"], f["kind"], f["ord"], f["raw"]) for f in frames]
        + [
            ("channel:0", "channel", 1, ch.SerializeToString()),
            ("config:lora", "config", 2, cfg.SerializeToString()),
        ]
    )
    assert http.get("/api/channels").json()[0]["name"] == "MediumFast"


def test_status_carries_the_local_nodes_names(api):
    http, _ = api
    link = http.get("/api/status").json()["upstream"]
    assert (link["my_short_name"], link["my_long_name"]) == ("HOME", "Home Base")


def test_message_details_resolve_the_relayer(api):
    http, vnode = api
    pkt = mesh_pb2.MeshPacket(id=0x800, to=proto.BROADCAST_NUM)
    pkt.__setattr__("from", ME)
    pkt.decoded.portnum = proto.PORT_TEXT
    pkt.decoded.payload = b"anyone?"
    seq = vnode.db.store_packet(
        raw=proto.wrap_packet(pkt), meta={**proto.packet_meta(pkt), "origin": "webui"}
    )
    vnode.db.log_delivery(0x800, ME, "implicit_ack", ack_from=ME, relay_node=FRIEND & 0xFF)
    details = http.get(f"/api/messages/{seq}").json()
    assert details["recipient"] is None
    [entry] = details["delivery"]
    assert entry["ack_from_name"]["short"] == "HOME"
    assert [c["short"] for c in entry["relay_candidates"]] == ["RR"]


def test_message_details_404_for_an_unknown_seq(api):
    http, _ = api
    assert http.get("/api/messages/999999").status_code == 404


# ------------------------------------------------------------ upstream link


def test_a_dead_reader_thread_is_noticed():
    iface = _Interface.__new__(_Interface)  # no connect
    iface._rxThread = threading.Thread(target=lambda: None)
    iface._rxThread.start()
    iface._rxThread.join()
    assert not iface.reader_alive()


def test_position_reports_carry_their_precision():
    pos = mesh_pb2.Position(latitude_i=514769000, longitude_i=-5000, precision_bits=13, time=1000)
    fields = proto.position_fields(pos, heard_at=2000)
    assert fields["precision_bits"] == 13
    assert fields["position_time"] == 1000


# ------------------------------------------------------------ channel info


def channel(index, role, *, name="", psk=b"\x01", precision=0):
    from meshtastic.protobuf import channel_pb2

    ch = channel_pb2.Channel(index=index, role=role)
    ch.settings.name = name
    ch.settings.psk = psk
    ch.settings.module_settings.position_precision = precision
    return ch


def test_a_default_key_channel_is_public_and_capped():
    [info] = proto.channel_info([channel(0, 1, precision=32)], lora())
    assert (info["key"], info["public"]) == ("default", True)
    # The firmware never shares more than 15 bits on a public channel.
    assert info["position_precision"] == 15


def test_a_private_channel_keeps_its_precision():
    [info] = proto.channel_info([channel(0, 1, psk=bytes(32), precision=32)], lora())
    assert (info["key"], info["public"], info["position_precision"]) == ("aes256", False, 32)


def test_a_secondary_without_a_key_inherits_the_primarys():
    chans = [channel(0, 1, psk=bytes(16)), channel(1, 2, name="team", psk=b"")]
    assert proto.channel_info(chans, lora())[1]["key"] == "aes128"


def test_position_goes_out_on_the_first_channel_that_shares_it():
    chans = [channel(0, 1), channel(1, 2, name="team", psk=bytes(32), precision=16)]
    infos = proto.channel_info(chans, lora())
    assert [c["carries_position"] for c in infos] == [False, True]


def test_disabled_channels_are_left_out():
    assert proto.channel_info([channel(0, 1), channel(1, 0)], lora())[-1]["index"] == 0


# ------------------------------------------------------------------ tracks


def test_a_track_skips_reports_that_did_not_move(tmp_path):
    from mesh_vnode.db import Database

    db = Database(tmp_path / "p.sqlite3")
    here = {"latitude": 51.4769, "longitude": -0.0005, "position_time": 100}
    assert db.store_position(FRIEND, here)
    assert not db.store_position(FRIEND, {**here, "position_time": 200})
    assert db.store_position(FRIEND, {**here, "latitude": 51.48, "position_time": 300})
    assert [r["time"] for r in db.track(FRIEND, 0)] == [100, 300]


def test_favourites_get_a_track_and_others_do_not(up):  # noqa: F811
    up._favorites = {FRIEND}
    for num in (FRIEND, 0x55EE66FF):
        pkt = mesh_pb2.MeshPacket(to=proto.BROADCAST_NUM, rx_time=1000)
        pkt.__setattr__("from", num)
        pkt.decoded.portnum = proto.PORT_POSITION
        pkt.decoded.payload = mesh_pb2.Position(
            latitude_i=514769000, longitude_i=-5000, precision_bits=32
        ).SerializeToString()
        store(up, pkt)
    assert len(up.db.track(FRIEND, 0)) == 1
    assert up.db.track(0x55EE66FF, 0) == []


def test_the_track_endpoint(api):
    http, vnode = api
    vnode.db.store_position(FRIEND, {"latitude": 51.4769, "longitude": -0.0005})
    [point] = http.get(f"/api/nodes/{FRIEND}/track").json()
    assert point["latitude"] == 51.4769


# ------------------------------------------------------------- admin pref


def test_apps_can_be_allowed_to_change_settings_from_the_web_ui(api):
    http, vnode = api
    assert http.get("/api/prefs").json()["allow_admin"] is False
    assert http.put("/api/prefs", json={"allow_admin": True}).json()["allow_admin"] is True
    assert vnode.settings.allow_admin is True
    http.put("/api/prefs", json={"allow_admin": None})
    assert vnode.settings.allow_admin is False  # back to VNODE_ALLOW_ADMIN


# ------------------------------------------------------------ self-connect


def test_our_own_virtual_node_is_recognised():
    from mesh_vnode.upstream import is_own_virtual_node

    assert is_own_virtual_node("127.0.0.1", 4404, 4404)
    assert is_own_virtual_node("localhost", 4404, 4404)
    assert not is_own_virtual_node("127.0.0.1", 4403, 4404)  # a node on this machine
    assert not is_own_virtual_node("192.0.2.10", 4404, 4404)  # not an address here
    assert not is_own_virtual_node("does-not-resolve.invalid", 4404, 4404)


def test_the_web_ui_refuses_our_own_virtual_node(api):
    http, vnode = api
    res = http.put("/api/prefs", json={"upstream_host": "127.0.0.1", "upstream_port": 4404})
    assert res.status_code == 422
    assert "own virtual node" in res.json()["detail"]
    assert vnode.settings.upstream_host == "192.0.2.10"
    assert "upstream_host" not in vnode.db.prefs()


# ------------------------------------------------------------- clear data


def test_clearing_resets_everything_but_settings_and_config(api):
    http, vnode = api
    db = vnode.db
    pkt = mesh_pb2.MeshPacket(id=0x900, to=proto.BROADCAST_NUM)
    pkt.__setattr__("from", FRIEND)
    pkt.decoded.portnum = proto.PORT_TEXT
    pkt.decoded.payload = b"old"
    db.store_packet(raw=proto.wrap_packet(pkt), meta=proto.packet_meta(pkt))
    db.store_position(FRIEND, {"latitude": 51.4769, "longitude": -0.0005})
    db.get_or_create_client("192.0.2.5")
    db.set_pref("carto_api_key", "abc")
    dropped = []
    vnode.server.disconnect_all = lambda: dropped.append(True) or 1

    res = http.post("/api/clear", json={"confirm": "CLEAR"})

    assert res.status_code == 200
    assert res.json()["removed"]["connected apps"] == 1
    assert dropped == [True]
    assert db.counts()["texts"] == 0
    assert db.nodes() == []
    assert db.track(FRIEND, 0) == []
    assert db.clients() == []
    assert db.prefs()["carto_api_key"] == "abc"
    assert db.config_frames(["my_info"])  # replaced by the next capture, not emptied
    assert vnode.upstream._kick.is_set()  # re-reads the node now


def test_numbering_starts_again_at_one_after_clearing(api):
    _, vnode = api
    db = vnode.db
    pkt = mesh_pb2.MeshPacket(id=0x901, to=proto.BROADCAST_NUM)
    pkt.__setattr__("from", FRIEND)
    pkt.decoded.portnum = proto.PORT_TEXT
    for i in range(3):
        pkt.id = 0x901 + i
        db.store_packet(raw=proto.wrap_packet(pkt), meta=proto.packet_meta(pkt))
    db.clear_data()
    pkt.id = 0x9FF
    assert db.store_packet(raw=proto.wrap_packet(pkt), meta=proto.packet_meta(pkt)) == 1


def test_clearing_needs_the_typed_confirmation(api):
    http, vnode = api
    assert http.post("/api/clear", json={"confirm": "yes"}).status_code == 422
    assert http.post("/api/clear", json={}).status_code == 422
    assert not vnode.upstream._kick.is_set()


def test_the_clear_preview_counts_what_would_go(api):
    http, vnode = api
    vnode.db.get_or_create_client("192.0.2.5")
    preview = http.get("/api/clear").json()
    assert preview["nodes"] == 2  # HOME and RR from the fixture
    assert preview["clients"] == 1
    assert preview["connected apps"] == 0
    assert vnode.db.nodes()  # a preview deletes nothing
