"""Tests for codec.osc — encode/decode, chunking, reassembly."""
import base64
import json
import re

import pytest

from webui_protocol.osc import (
    IDENTITY,
    CHUNK_SIZE,
    ChunkReassembler,
    decode_osc_payload,
    encode_osc,
    make_chunks,
)

_OSC_RE = re.compile(
    r"\x1b\]1337;Custom=id=" + re.escape(IDENTITY) + r":([A-Za-z0-9+/=.]+)\x07"
)


def _parse_osc(osc: str) -> tuple[dict, bytes | None] | None:
    """Extract and decode the payload from an OSC sequence."""
    m = _OSC_RE.search(osc)
    assert m, f"No OSC sequence found in {osc!r}"
    return decode_osc_payload(m.group(1))


# ---------------------------------------------------------------------------
# encode / decode
# ---------------------------------------------------------------------------


def test_encode_produces_osc_wrapper():
    osc = encode_osc({"type": "init"})
    assert osc.startswith("\x1b]1337;Custom=id=webui-bridge:")
    assert osc.endswith("\x07")


def test_encode_decode_roundtrip_header_only():
    header = {"type": "init"}
    result = _parse_osc(encode_osc(header))
    assert result is not None
    decoded_header, decoded_body = result
    assert decoded_header == header
    assert decoded_body is None


def test_encode_decode_roundtrip_with_body():
    header = {"type": "http_response", "request_id": "abc", "status": 200, "headers": {}}
    body = b'{"key": "value"}'
    result = _parse_osc(encode_osc(header, body))
    assert result is not None
    decoded_header, decoded_body = result
    assert decoded_header == header
    assert decoded_body == body


def test_decode_returns_none_on_garbage():
    assert decode_osc_payload("!!!not-base64!!!") is None
    assert decode_osc_payload(base64.b64encode(b"not json").decode()) is None


def test_unicode_header_survives_roundtrip():
    header = {"type": "init", "msg": "你好，世界 🌍"}
    result = _parse_osc(encode_osc(header))
    assert result is not None
    assert result[0]["msg"] == header["msg"]


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------


def test_single_chunk_for_small_body():
    header = {"type": "http_response", "status": 200, "headers": {}}
    body = b"small"
    chunks = make_chunks(header, body, "msg-1")
    assert len(chunks) == 1
    chunk_header, chunk_body = chunks[0]
    assert chunk_header["_chunk"]["seq"] == 0
    assert chunk_header["_chunk"]["total"] == 1
    assert chunk_header["_chunk"]["msg_id"] == "msg-1"
    assert chunk_body == body


def test_multiple_chunks_for_large_body():
    header = {"type": "http_response", "status": 200, "headers": {}}
    body = b"x" * (CHUNK_SIZE * 3 + 100)
    chunks = make_chunks(header, body, "msg-big")
    assert len(chunks) == 4
    for i, (ch, cb) in enumerate(chunks):
        if i == 0:
            assert ch["_chunk"]["seq"] == 0
        else:
            assert ch["type"] == "chunk"
            assert ch["seq"] == i
            assert ch["msg_id"] == "msg-big"


# ---------------------------------------------------------------------------
# reassembly
# ---------------------------------------------------------------------------


def test_reassemble_single_chunk():
    original_header = {"type": "http_response", "status": 200, "headers": {}}
    body = b"hello"
    chunks = make_chunks(original_header, body, "m1")
    r = ChunkReassembler()
    ch, cb = chunks[0]
    result = r.feed("sess-1", ch, cb)
    assert result is not None
    reassembled_header, reassembled_body = result
    assert reassembled_header == original_header
    assert reassembled_body == body


def test_reassemble_multiple_chunks_in_order():
    original_header = {"type": "http_response", "status": 200, "headers": {}}
    body = b"a" * (CHUNK_SIZE * 3 + 50)
    chunks = make_chunks(original_header, body, "m2")
    r = ChunkReassembler()
    result = None
    for ch, cb in chunks:
        result = r.feed("sess-1", ch, cb)
    assert result is not None
    assert result[0] == original_header
    assert result[1] == body


def test_reassemble_out_of_order():
    original_header = {"type": "data", "status": 200, "headers": {}}
    body = b"b" * (CHUNK_SIZE * 3 + 50)
    chunks = make_chunks(original_header, body, "m3")
    r = ChunkReassembler()
    result = None
    for ch, cb in reversed(chunks):
        result = r.feed("sess-1", ch, cb)
    assert result is not None
    assert result[1] == body


def test_reassembly_isolates_sessions():
    """Chunks from different sessions must not mix."""
    h1 = {"type": "resp", "headers": {}}
    h2 = {"type": "resp", "headers": {}}
    body1 = b"s1" * (CHUNK_SIZE + 1)
    body2 = b"s2" * (CHUNK_SIZE + 1)
    c1 = make_chunks(h1, body1, "same-msg-id")
    c2 = make_chunks(h2, body2, "same-msg-id")
    assert len(c1) > 1 and len(c2) > 1

    r = ChunkReassembler()
    result1 = result2 = None
    for (ch1, cb1), (ch2, cb2) in zip(c1, c2):
        result1 = r.feed("sess-A", ch1, cb1)
        result2 = r.feed("sess-B", ch2, cb2)

    assert result1 is not None and result1[1] == body1
    assert result2 is not None and result2[1] == body2


def test_reassembly_cleans_up_after_completion():
    original_header = {"type": "data", "headers": {}}
    body = b"c" * (CHUNK_SIZE + 1)
    chunks = make_chunks(original_header, body, "m4")
    r = ChunkReassembler()
    for ch, cb in chunks:
        r.feed("sess-1", ch, cb)
    assert "sess-1" not in r._pending or "m4" not in r._pending.get("sess-1", {})
