"""Which portnums a client may push upstream.

MeshMonitor's block list carries a mislabelled comment (it calls portnum 8
"NODEINFO_APP"; 8 is WAYPOINT_APP, NODEINFO_APP is 4). These assertions pin the
numbers down so the same mistake cannot creep back in.

MeshMonitor: https://github.com/Yeraze/meshmonitor
"""

from meshtastic.protobuf import portnums_pb2

from mesh_vnode import protocol as proto


def test_portnum_constants_match_the_firmware_enum():
    assert proto.PORT_TEXT == 1
    assert proto.PORT_POSITION == 3
    assert proto.PORT_NODEINFO == 4
    assert proto.PORT_ADMIN == 6
    assert proto.PORT_TELEMETRY == 67
    assert portnums_pb2.PortNum.Value("WAYPOINT_APP") == 8


def test_only_admin_is_blocked():
    assert proto.BLOCKED_PORTNUMS == (proto.PORT_ADMIN,)


def test_node_info_and_waypoints_are_not_blocked():
    # Node info is how a node's name reaches the mesh; waypoints are user content.
    assert proto.PORT_NODEINFO not in proto.BLOCKED_PORTNUMS
    assert portnums_pb2.PortNum.Value("WAYPOINT_APP") not in proto.BLOCKED_PORTNUMS


def test_text_is_never_blocked():
    assert proto.PORT_TEXT not in proto.BLOCKED_PORTNUMS
