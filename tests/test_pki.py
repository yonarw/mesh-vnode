"""How a from=0 packet is handled on its way upstream.

The default must never weaken a direct message: a DM through this service should
be exactly as private as one sent from the app connected straight to the node.
"""

from meshtastic.protobuf import mesh_pb2

from mesh_vnode import protocol as proto

MY_NUM = 0x433A1B2C


def sealed_dm(from_num: int = 0) -> bytes:
    tr = mesh_pb2.ToRadio()
    tr.packet.__setattr__("from", from_num)
    tr.packet.to = 0x11AA22BB
    tr.packet.id = 0xABCD
    tr.packet.pki_encrypted = True
    tr.packet.public_key = b"\x02" * 32
    tr.packet.decoded.portnum = proto.PORT_TEXT
    tr.packet.decoded.payload = b"private"
    return tr.SerializeToString()


def test_fill_from_stamps_the_node_number_and_keeps_the_seal():
    out = proto.parse_to_radio(proto.fill_from_if_zero(sealed_dm(), MY_NUM))
    assert out.packet.__getattribute__("from") == MY_NUM
    assert out.packet.pki_encrypted is True
    assert out.packet.public_key == b"\x02" * 32
    assert out.packet.decoded.payload == b"private"


def test_fill_from_leaves_an_already_stamped_packet_alone():
    original = sealed_dm(from_num=0x999)
    assert proto.fill_from_if_zero(original, MY_NUM) is original


def test_fill_from_without_a_known_node_number_is_a_no_op():
    original = sealed_dm()
    assert proto.fill_from_if_zero(original, 0) is original


def test_the_fix_does_not_disturb_an_unsealed_broadcast():
    tr = mesh_pb2.ToRadio()
    tr.packet.to = proto.BROADCAST_NUM
    tr.packet.id = 1
    tr.packet.decoded.portnum = proto.PORT_TEXT
    tr.packet.decoded.payload = b"hello"
    original = tr.SerializeToString()
    # Stamping the sender is harmless and correct here too.
    filled = proto.parse_to_radio(proto.fill_from_if_zero(original, MY_NUM))
    assert filled.packet.__getattribute__("from") == MY_NUM
    assert filled.packet.pki_encrypted is False
