"""OSC 1337 custom-sequence encode/decode and chunked binary transport.

Wire format (the portion after ``Custom=id=webgui-bridge:``):

    <base64(compact-json-header)>[.<base64(body)>]

The ``.`` separator is not in the base64 alphabet, so splitting on the first
``.`` unambiguously separates header from body.  The body section is omitted
entirely when there is nothing to send (empty body or no binary payload).

Chunking (when body bytes exceed CHUNK_SIZE):

    First  OSC: header = {…original fields…, "_chunk": {"msg_id":"…","seq":0,"total":N}},
                body   = body[0 : CHUNK_SIZE]
    Next   OSC: header = {"type":"chunk","msg_id":"…","seq":i,"total":N},
                body   = body[i*CHUNK_SIZE : (i+1)*CHUNK_SIZE]

The receiver reassembles the raw body slices and dispatches with the original
header once all N chunks have arrived.  The header JSON is never split.
"""
import base64
import json
import uuid

IDENTITY = "webgui-bridge"
CHUNK_SIZE = 16 * 1024

# ``"."`` is not in the standard base64 alphabet (A-Za-z0-9+/=).
_SEP = "."


def encode_osc(header: dict, body: bytes | None = None) -> str:
    """Encode *header* (and optional *body*) as an OSC 1337 custom sequence."""
    h = base64.b64encode(json.dumps(header, separators=(",", ":")).encode()).decode()
    if body:
        b = base64.b64encode(body).decode()
        return f"\033]1337;Custom=id={IDENTITY}:{h}{_SEP}{b}\007"
    return f"\033]1337;Custom=id={IDENTITY}:{h}\007"


def decode_osc_payload(raw: str) -> tuple[dict, bytes | None] | None:
    """Decode the OSC payload string (everything after ``id=webgui-bridge:``).

    Returns ``(header_dict, body_bytes)`` or ``None`` on parse failure.
    ``body_bytes`` is ``None`` when no body section is present.
    """
    parts = raw.split(_SEP, 1)
    try:
        header = json.loads(base64.b64decode(parts[0]).decode())
    except Exception:
        return None
    body: bytes | None = None
    if len(parts) > 1:
        try:
            body = base64.b64decode(parts[1])
        except Exception:
            return None
    return header, body


def make_chunks(
    header: dict, body: bytes, msg_id: str | None = None
) -> list[tuple[dict, bytes]]:
    """Split *body* into ≤CHUNK_SIZE slices.

    Returns a list of ``(chunk_header, chunk_body)`` pairs ready for
    ``encode_osc``.  The first pair carries the full original *header* plus
    ``"_chunk"`` metadata; subsequent pairs use ``{"type":"chunk", …}``.
    """
    if msg_id is None:
        msg_id = uuid.uuid4().hex
    parts = [body[i : i + CHUNK_SIZE] for i in range(0, len(body), CHUNK_SIZE)]
    total = len(parts)
    result: list[tuple[dict, bytes]] = [
        ({**header, "_chunk": {"msg_id": msg_id, "seq": 0, "total": total}}, parts[0])
    ]
    for i, part in enumerate(parts[1:], 1):
        result.append(({"type": "chunk", "msg_id": msg_id, "seq": i, "total": total}, part))
    return result


class ChunkReassembler:
    """Reassemble chunked binary bodies, keyed by (session_id, msg_id)."""

    def __init__(self) -> None:
        # session_id → msg_id → {"header": dict|None, "parts": {seq: bytes}, "total": int}
        self._pending: dict[str, dict[str, dict]] = {}

    def feed(
        self, session_id: str, header: dict, body: bytes | None
    ) -> tuple[dict, bytes | None] | None:
        """Feed one ``(header, body)`` pair from a decoded OSC sequence.

        Returns ``(original_header, full_body)`` when all chunks have arrived,
        ``None`` otherwise.
        """
        chunk_meta = header.get("_chunk")

        if chunk_meta:
            msg_id = chunk_meta["msg_id"]
            total = chunk_meta["total"]
            original_header = {k: v for k, v in header.items() if k != "_chunk"}
            entry = self._pending.setdefault(session_id, {}).setdefault(msg_id, {
                "header": original_header,
                "parts": {},
                "total": total,
            })
            entry["header"] = original_header
            entry["parts"][0] = body or b""

        elif header.get("type") == "chunk":
            msg_id = header["msg_id"]
            seq = header["seq"]
            total = header["total"]
            session_map = self._pending.setdefault(session_id, {})
            if msg_id not in session_map:
                session_map[msg_id] = {"header": None, "parts": {}, "total": total}
            entry = session_map[msg_id]
            entry["parts"][seq] = body or b""

        else:
            return None

        if entry.get("header") is not None and len(entry["parts"]) == entry["total"]:
            return self._assemble(session_id, msg_id, entry)
        return None

    def _assemble(
        self, session_id: str, msg_id: str, entry: dict
    ) -> tuple[dict, bytes | None]:
        full = b"".join(entry["parts"][i] for i in range(entry["total"]))
        self._pending.get(session_id, {}).pop(msg_id, None)
        return entry["header"], full or None
