import pytest

from mesh_vnode.framing import MAX_PAYLOAD, FrameDecoder, encode_frame


def test_round_trip():
    payload = b"hello mesh"
    assert FrameDecoder().feed(encode_frame(payload)) == [payload]


def test_multiple_frames_in_one_read():
    data = encode_frame(b"one") + encode_frame(b"two") + encode_frame(b"three")
    assert FrameDecoder().feed(data) == [b"one", b"two", b"three"]


def test_split_across_reads():
    frame = encode_frame(b"split me up")
    decoder = FrameDecoder()
    out = []
    for i in range(len(frame)):
        out += decoder.feed(frame[i : i + 1])
    assert out == [b"split me up"]


def test_header_split_between_reads():
    """The 0x94 0xC3 pair arriving one byte per read must not be lost."""
    frame = encode_frame(b"payload")
    decoder = FrameDecoder()
    assert decoder.feed(frame[:1]) == []
    assert decoder.feed(frame[1:]) == [b"payload"]


def test_log_noise_before_a_frame_is_dropped():
    """The firmware interleaves plain-text log lines with frames."""
    data = b"INFO  some log line\n" + encode_frame(b"real")
    assert FrameDecoder().feed(data) == [b"real"]


def test_bogus_length_resynchronises():
    bad = bytes([0x94, 0xC3, 0xFF, 0xFF])
    assert FrameDecoder().feed(bad + encode_frame(b"good")) == [b"good"]


def test_oversized_payload_rejected():
    with pytest.raises(ValueError):
        encode_frame(b"x" * (MAX_PAYLOAD + 1))
