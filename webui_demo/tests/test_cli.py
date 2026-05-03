"""Tests for demo/cli.py HTTP route handlers and WebSocket callbacks."""
import json
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli as cli_mod
from cli import app, Todo, _todos, _next_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(method: str, path: str, body: bytes = b""):
    """Construct a codec.bridge.Request directly from header + body."""
    from webui_protocol.bridge import Request
    return Request(
        {"method": method, "path": path, "headers": {}},
        body if body else None,
    )


def _call_route(method: str, path: str, body: bytes = b""):
    """Dispatch a request through the bridge's router and return the Response."""
    import asyncio
    req = _make_request(method, path, body)
    return asyncio.get_event_loop().run_until_complete(app._dispatch_http(req))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_todos():
    """Reset todo state to a known baseline before each test."""
    _todos.clear()
    _todos.extend([
        Todo(1, "Task A"),
        Todo(2, "Task B", done=True),
    ])
    cli_mod._next_id = 3
    yield
    _todos.clear()


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------


async def test_get_root_returns_html():
    from webui_protocol.bridge import Request
    req = _make_request("GET", "/")
    resp = await app._dispatch_http(req)
    assert resp.status == 200
    assert "text/html" in resp.headers["Content-Type"]
    assert b"TODO" in resp.body
    assert b"<script>" in resp.body


async def test_get_index_html_alias():
    req = _make_request("GET", "/index.html")
    resp = await app._dispatch_http(req)
    assert resp.status == 200


# ---------------------------------------------------------------------------
# GET /api/todos
# ---------------------------------------------------------------------------


async def test_list_todos():
    req = _make_request("GET", "/api/todos")
    resp = await app._dispatch_http(req)
    assert resp.status == 200
    data = json.loads(resp.body)
    assert len(data["todos"]) == 2
    assert data["todos"][0]["id"] == 1
    assert data["todos"][1]["done"] is True


# ---------------------------------------------------------------------------
# POST /api/todos
# ---------------------------------------------------------------------------


async def test_create_todo():
    req = _make_request("POST", "/api/todos", b'{"text":"New task"}')
    resp = await app._dispatch_http(req)
    assert resp.status == 200
    data = json.loads(resp.body)
    texts = [t["text"] for t in data["todos"]]
    assert "New task" in texts


async def test_create_todo_empty_text_returns_400():
    req = _make_request("POST", "/api/todos", b'{"text":"  "}')
    resp = await app._dispatch_http(req)
    assert resp.status == 400


# ---------------------------------------------------------------------------
# PATCH /api/todos/:id
# ---------------------------------------------------------------------------


async def test_toggle_todo_false_to_true():
    req = _make_request("PATCH", "/api/todos/1")
    resp = await app._dispatch_http(req)
    data = json.loads(resp.body)
    task = next(t for t in data["todos"] if t["id"] == 1)
    assert task["done"] is True


async def test_toggle_todo_true_to_false():
    req = _make_request("PATCH", "/api/todos/2")
    resp = await app._dispatch_http(req)
    data = json.loads(resp.body)
    task = next(t for t in data["todos"] if t["id"] == 2)
    assert task["done"] is False


async def test_toggle_nonexistent_returns_404():
    req = _make_request("PATCH", "/api/todos/999")
    resp = await app._dispatch_http(req)
    assert resp.status == 404


# ---------------------------------------------------------------------------
# DELETE /api/todos/:id
# ---------------------------------------------------------------------------


async def test_delete_todo():
    req = _make_request("DELETE", "/api/todos/1")
    resp = await app._dispatch_http(req)
    assert resp.status == 200
    data = json.loads(resp.body)
    ids = [t["id"] for t in data["todos"]]
    assert 1 not in ids


async def test_delete_nonexistent_returns_404():
    req = _make_request("DELETE", "/api/todos/999")
    resp = await app._dispatch_http(req)
    assert resp.status == 404


# ---------------------------------------------------------------------------
# Unknown routes
# ---------------------------------------------------------------------------


async def test_unknown_path_returns_404():
    req = _make_request("GET", "/nonexistent")
    resp = await app._dispatch_http(req)
    assert resp.status == 404
