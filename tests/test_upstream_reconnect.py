"""The upstream link against a node that drops it, over real sockets.

A node serves one TCP client and drops the current one when another connects.
The link must come back with a fresh handshake, and must not leave a second
socket behind - one that would hold the node's only slot.
"""

import socket
import threading
import time

from meshtastic.protobuf import mesh_pb2

from mesh_vnode.config import Settings
from mesh_vnode.db import Database
from mesh_vnode.fakenode import build_handshake
from mesh_vnode.framing import FrameDecoder, encode_frame
from mesh_vnode.upstream import Upstream


class DroppingNode:
    """Answers handshakes; `drop()` closes the current client like the firmware."""

    def __init__(self) -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.accepted = 0
        self.handshakes = 0
        self.open: set[socket.socket] = set()
        self._lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with self._lock:
                self.accepted += 1
                self.open.add(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        decoder = FrameDecoder()
        try:
            while data := conn.recv(4096):
                for payload in decoder.feed(data):
                    tr = mesh_pb2.ToRadio.FromString(payload)
                    if tr.WhichOneof("payload_variant") == "want_config_id":
                        self.handshakes += 1
                        for frame in build_handshake(tr.want_config_id):
                            conn.sendall(encode_frame(frame))
        except OSError:
            pass
        with self._lock:
            self.open.discard(conn)
        conn.close()

    def drop(self) -> None:
        with self._lock:
            for conn in self.open:
                conn.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        self.server.close()
        self.drop()


def wait_for(cond, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_reconnects_once_with_fresh_handshake_after_node_drops_link(tmp_path):
    node = DroppingNode()
    db = Database(tmp_path / "u.sqlite3")
    up = Upstream(Settings(db_path=tmp_path / "u.sqlite3"), db, "127.0.0.1", node.port)
    up.start()
    try:
        assert wait_for(lambda: up.connected.is_set() and node.handshakes == 1)
        first = up.iface

        node.drop()

        assert wait_for(lambda: node.handshakes == 2 and up.connected.is_set())
        assert up.iface is not first
        # Settled: one connection per handshake, and only one still open.
        time.sleep(1.5)
        assert node.accepted == 2
        assert len(node.open) == 1
    finally:
        up.stop()
        node.close()
        db.close()


def test_failed_heartbeat_hands_over_to_the_supervisor(tmp_path):
    node = DroppingNode()
    db = Database(tmp_path / "h.sqlite3")
    up = Upstream(Settings(db_path=tmp_path / "h.sqlite3"), db, "127.0.0.1", node.port)
    up.start()
    try:
        assert wait_for(lambda: up.connected.is_set() and node.handshakes == 1)
        first = up.iface
        # The heartbeat timer hitting a broken pipe, racing the reader.
        first.socket.shutdown(socket.SHUT_WR)
        first.sendHeartbeat()

        assert wait_for(lambda: node.handshakes == 2 and up.connected.is_set())
        assert up.iface is not first
        time.sleep(1.5)
        assert node.accepted == 2
    finally:
        up.stop()
        node.close()
        db.close()
