from meshtastic.protobuf import mesh_pb2, telemetry_pb2

from mesh_vnode import protocol as proto


def _text_packet(text="hi", frm=0x1234, to=proto.BROADCAST_NUM):
    pkt = mesh_pb2.MeshPacket()
    pkt.__setattr__("from", frm)
    pkt.to = to
    pkt.id = 42
    pkt.rx_time = 1700000000
    pkt.hop_start = 3
    pkt.hop_limit = 2
    pkt.decoded.portnum = proto.PORT_TEXT
    pkt.decoded.payload = text.encode()
    return pkt


def test_packet_meta_extracts_text():
    meta = proto.packet_meta(_text_packet("hello"))
    assert meta["text"] == "hello"
    assert meta["portnum"] == proto.PORT_TEXT
    assert meta["from_num"] == 0x1234
    assert meta["hop_start"] == 3


def test_config_complete_round_trips():
    fr = proto.parse_from_radio(proto.build_config_complete(4242))
    assert fr.config_complete_id == 4242


def test_queue_status_round_trips():
    fr = proto.parse_from_radio(proto.build_queue_status(free=7, maxlen=9))
    assert fr.queueStatus.free == 7
    assert fr.queueStatus.maxlen == 9


def test_config_frame_keys_are_stable():
    fr = mesh_pb2.FromRadio()
    fr.channel.index = 2
    assert proto.config_frame_key(fr) == ("channel:2", "channel")

    fr = mesh_pb2.FromRadio()
    fr.config.lora.hop_limit = 3
    assert proto.config_frame_key(fr) == ("config:lora", "config")

    fr = mesh_pb2.FromRadio()
    fr.node_info.num = 99
    assert proto.config_frame_key(fr) == ("node_info:99", "node_info")

    fr = mesh_pb2.FromRadio()
    fr.packet.id = 1
    assert proto.config_frame_key(fr) is None


def test_decode_telemetry_device_metrics():
    pkt = mesh_pb2.MeshPacket()
    pkt.decoded.portnum = proto.PORT_TELEMETRY
    tel = telemetry_pb2.Telemetry()
    tel.device_metrics.battery_level = 77
    tel.device_metrics.voltage = 3.95
    pkt.decoded.payload = tel.SerializeToString()

    kind, values = proto.decode_telemetry(pkt)
    assert kind == "device"
    assert values["battery_level"] == 77
    assert values["voltage"] == 3.95


def test_node_id_formats_like_the_apps():
    assert proto.node_id(0x433A1B2C) == "!433a1b2c"


def _admin(**variant):
    from meshtastic.protobuf import admin_pb2

    pkt = mesh_pb2.MeshPacket()
    pkt.decoded.portnum = proto.PORT_ADMIN
    pkt.decoded.payload = admin_pb2.AdminMessage(**variant).SerializeToString()
    return pkt


def test_admin_requests_are_named_for_the_log():
    from meshtastic.protobuf import admin_pb2

    cfg = admin_pb2.AdminMessage.ConfigType.SESSIONKEY_CONFIG
    assert proto.describe_admin(_admin(get_config_request=cfg)) == (
        "get_config_request",
        "get_config_request=SESSIONKEY_CONFIG",
    )
    assert proto.describe_admin(_admin(get_owner_request=True)) == (
        "get_owner_request",
        "get_owner_request",
    )
    assert proto.describe_admin(_admin(set_time_only=1789830000))[0] == "set_time_only"


def test_an_admin_payload_newer_than_the_package_is_not_read_only():
    pkt = mesh_pb2.MeshPacket()
    pkt.decoded.portnum = proto.PORT_ADMIN
    pkt.decoded.payload = bytes([0xFA, 0x07, 0x00])  # field 127, unknown here
    variant, desc = proto.describe_admin(pkt)
    assert variant is None
    assert variant not in proto.READ_ONLY_ADMIN_VARIANTS
    assert "does not know" in desc


def test_only_get_requests_count_as_read_only():
    assert all(v.startswith("get_") for v in proto.READ_ONLY_ADMIN_VARIANTS)


def _heard(hop_start, hop_limit, via_mqtt=False):
    pkt = mesh_pb2.MeshPacket()
    pkt.hop_start = hop_start
    pkt.hop_limit = hop_limit
    pkt.via_mqtt = via_mqtt
    return proto.heard_fields(pkt)


def test_heard_fields_counts_hops_travelled():
    assert _heard(7, 2)["hops_away"] == 5
    assert _heard(3, 3)["hops_away"] == 0


def test_heard_fields_leaves_hops_alone_when_they_say_nothing():
    # Pre-2.2 firmware sends no hop_start, MQTT injects the counters untouched,
    # and hop_limit above hop_start is a packet we cannot read a distance from.
    assert "hops_away" not in _heard(0, 3)
    assert "hops_away" not in _heard(3, 3, via_mqtt=True)
    assert "hops_away" not in _heard(3, 5)
    assert _heard(3, 3, via_mqtt=True)["via_mqtt"] == 1


def test_refreshed_node_info_records_hops_from_a_live_packet():
    pkt = _text_packet(frm=0x1234)  # hop_start 3, hop_limit 2
    raw = mesh_pb2.FromRadio()
    raw.node_info.num = 0x1234
    raw.node_info.hops_away = 0
    info = proto.parse_from_radio(
        proto.refreshed_node_info(raw.SerializeToString(), pkt, now=1700000000)
    ).node_info
    assert info.hops_away == 1


def test_telemetry_request_is_not_a_reading():
    """A node asking for telemetry names the variant and leaves it empty."""
    pkt = mesh_pb2.MeshPacket()
    pkt.decoded.portnum = proto.PORT_TELEMETRY
    pkt.decoded.want_response = True
    tel = telemetry_pb2.Telemetry()
    tel.local_stats.SetInParent()
    pkt.decoded.payload = tel.SerializeToString()
    assert proto.decode_telemetry(pkt) is None
