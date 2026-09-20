"""Meshtastic stream framing: 0x94 0xC3, 2-byte big-endian length, protobuf payload.

Same wire format the firmware uses on serial and TCP, so the phone app and the
`meshtastic` python package both speak it unchanged.
"""

from __future__ import annotations

START1 = 0x94
START2 = 0xC3
HEADER_LEN = 4
MAX_PAYLOAD = 512


def encode_frame(payload: bytes) -> bytes:
    """Wrap a serialised protobuf in a stream frame."""
    n = len(payload)
    if n > MAX_PAYLOAD:
        raise ValueError(f"payload too large for a meshtastic frame: {n} > {MAX_PAYLOAD}")
    return bytes([START1, START2, (n >> 8) & 0xFF, n & 0xFF]) + payload


class FrameDecoder:
    """Incremental decoder. Feed it socket reads, get back complete payloads.

    Bytes that are not part of a frame are dropped: the firmware interleaves
    plain-text log lines with frames on serial, and a phone app can in principle
    do the same.
    """

    def __init__(self, max_payload: int = MAX_PAYLOAD) -> None:
        self._buf = bytearray()
        self._max_payload = max_payload

    def feed(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        out: list[bytes] = []
        while True:
            start = self._find_start()
            if start is None:
                # Keep a trailing byte: it may be the START1 of a split header.
                if self._buf and self._buf[-1] == START1:
                    del self._buf[:-1]
                else:
                    self._buf.clear()
                return out
            if start:
                del self._buf[:start]
            if len(self._buf) < HEADER_LEN:
                return out
            length = (self._buf[2] << 8) | self._buf[3]
            if length > self._max_payload:
                # Bogus length - drop this START1 and resynchronise.
                del self._buf[:1]
                continue
            end = HEADER_LEN + length
            if len(self._buf) < end:
                return out
            out.append(bytes(self._buf[HEADER_LEN:end]))
            del self._buf[:end]

    def _find_start(self) -> int | None:
        idx = self._buf.find(bytes([START1, START2]))
        return idx if idx >= 0 else None
