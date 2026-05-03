"""Tests for iterm2_webui.session_mgr — Future and Queue state machine."""
import asyncio

import pytest

from iterm2_webui.session_mgr import SessionManager


def _mgr() -> SessionManager:
    return SessionManager()


# ---------------------------------------------------------------------------
# HTTP futures
# ---------------------------------------------------------------------------


async def test_register_and_resolve_http():
    mgr = _mgr()
    rid = mgr.new_request_id()
    future = mgr.register_http(rid, "sess-1")

    assert mgr.get_http_session_id(rid) == "sess-1"

    header = {"status": 200, "headers": {}}
    body = b""
    assert mgr.resolve_http(rid, header, body) is True
    result_header, result_body = future.result()
    assert result_header == header
    assert result_body == body


async def test_resolve_http_returns_false_for_unknown_id():
    mgr = _mgr()
    assert mgr.resolve_http("no-such-id", {}, None) is False


async def test_reject_http_sets_exception():
    mgr = _mgr()
    rid = mgr.new_request_id()
    future = mgr.register_http(rid, "sess-1")

    mgr.reject_http(rid, TimeoutError("boom"))
    with pytest.raises(TimeoutError):
        future.result()


async def test_resolve_is_idempotent():
    """Resolving an already-resolved future should not raise."""
    mgr = _mgr()
    rid = mgr.new_request_id()
    mgr.register_http(rid, "sess-1")
    mgr.resolve_http(rid, {"status": 200}, None)
    # Second call: rid already popped
    assert mgr.resolve_http(rid, {"status": 500}, None) is False


async def test_get_session_id_is_none_after_resolve():
    mgr = _mgr()
    rid = mgr.new_request_id()
    mgr.register_http(rid, "sess-x")
    mgr.resolve_http(rid, {}, None)
    assert mgr.get_http_session_id(rid) is None


# ---------------------------------------------------------------------------
# WebSocket queues
# ---------------------------------------------------------------------------


async def test_register_and_use_ws():
    mgr = _mgr()
    conn_id = mgr.new_conn_id()
    queue = mgr.register_ws(conn_id, "sess-ws")

    assert mgr.get_ws_session_id(conn_id) == "sess-ws"
    assert mgr.get_ws_queue(conn_id) is queue

    await queue.put("hello")
    assert await queue.get() == "hello"


async def test_unregister_ws_cleans_up():
    mgr = _mgr()
    conn_id = mgr.new_conn_id()
    mgr.register_ws(conn_id, "sess-ws")
    mgr.unregister_ws(conn_id)

    assert mgr.get_ws_queue(conn_id) is None
    assert mgr.get_ws_session_id(conn_id) is None


async def test_multiple_ws_connections_are_independent():
    mgr = _mgr()
    c1 = mgr.new_conn_id()
    c2 = mgr.new_conn_id()
    assert c1 != c2

    q1 = mgr.register_ws(c1, "sess-1")
    q2 = mgr.register_ws(c2, "sess-2")
    assert q1 is not q2

    await q1.put("msg-for-1")
    assert q2.empty()
    assert await q1.get() == "msg-for-1"
