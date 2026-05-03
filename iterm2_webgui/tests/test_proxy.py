"""Integration tests for iterm2_webgui.proxy_server."""
import asyncio
import json
import re

import aiohttp
import pytest

from webgui_protocol.osc import IDENTITY, decode_osc_payload
from iterm2_webgui.proxy_server import ProxyServer
from iterm2_webgui.session_mgr import SessionManager

_OSC_RE = re.compile(
    r"\x1b\]1337;Custom=id=" + re.escape(IDENTITY) + r":([A-Za-z0-9+/=.]+)\x07"
)


def _parse_osc(text: str) -> tuple[dict, bytes | None] | None:
    m = _OSC_RE.search(text)
    return decode_osc_payload(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _MockSession:
    """Fake iterm2.Session that captures injected OSC text."""

    def __init__(self, session_id: str = "mock-session"):
        self.session_id = session_id
        self._sent: asyncio.Queue = asyncio.Queue()

    async def drain(self, timeout: float = 3.0) -> str:
        return await asyncio.wait_for(self._sent.get(), timeout=timeout)


@pytest.fixture
async def harness():
    """Start a ProxyServer with a mock session; yield (proxy, session, mgr, client, port)."""
    mgr = SessionManager()
    session = _MockSession()

    async def send_osc(sess, text: str) -> None:
        await sess._sent.put(text)

    proxy = ProxyServer(mgr, lambda: session, send_osc)
    port = await proxy.start()

    connector = aiohttp.TCPConnector()
    client = aiohttp.ClientSession(connector=connector)

    yield proxy, session, mgr, client, port

    await client.close()
    await proxy.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_init_page_served_directly(harness):
    proxy, session, mgr, client, port = harness
    async with client.get(f"http://127.0.0.1:{port}/_init") as resp:
        assert resp.status == 200
        assert "text/html" in resp.headers["Content-Type"]
        text = await resp.text()
    assert "WebGUI" in text


async def test_csp_header_present_on_init(harness):
    proxy, session, mgr, client, port = harness
    async with client.get(f"http://127.0.0.1:{port}/_init") as resp:
        csp = resp.headers.get("Content-Security-Policy", "")
    assert "127.0.0.1" in csp


async def test_http_roundtrip_json(harness):
    """Proxy forwards request upstream and returns the remote CLI's response."""
    proxy, session, mgr, client, port = harness

    req_task = asyncio.create_task(
        client.get(f"http://127.0.0.1:{port}/api/todos")
    )

    osc_text = await session.drain()
    result = _parse_osc(osc_text)
    assert result is not None
    upstream_header, upstream_body = result
    assert upstream_header["type"] == "http_request"
    assert upstream_header["method"] == "GET"
    assert upstream_header["path"] == "/api/todos"
    rid = upstream_header["request_id"]

    body = json.dumps({"todos": []}).encode()
    mgr.resolve_http(rid, {
        "status": 200,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
    }, body)

    resp = await asyncio.wait_for(req_task, timeout=3)
    assert resp.status == 200
    assert "application/json" in resp.headers.get("Content-Type", "")
    data = await resp.json()
    assert data == {"todos": []}


async def test_http_roundtrip_html(harness):
    proxy, session, mgr, client, port = harness

    req_task = asyncio.create_task(client.get(f"http://127.0.0.1:{port}/"))
    osc_text = await session.drain()
    result = _parse_osc(osc_text)
    assert result is not None
    rid = result[0]["request_id"]

    html = b"<html><body>hello</body></html>"
    mgr.resolve_http(rid, {
        "status": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
    }, html)

    resp = await asyncio.wait_for(req_task, timeout=3)
    assert resp.status == 200
    assert "text/html" in resp.headers["Content-Type"]
    text = await resp.text()
    assert text == html.decode()


async def test_csp_header_overrides_remote_policy(harness):
    """Even if the remote returns its own CSP, the proxy replaces it."""
    proxy, session, mgr, client, port = harness

    req_task = asyncio.create_task(client.get(f"http://127.0.0.1:{port}/page"))
    osc_text = await session.drain()
    rid = _parse_osc(osc_text)[0]["request_id"]

    mgr.resolve_http(rid, {
        "status": 200,
        "headers": {
            "Content-Type": "text/html",
            "Content-Security-Policy": "default-src *",
        },
    }, b"ok")

    resp = await asyncio.wait_for(req_task, timeout=3)
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "default-src *" not in csp
    assert "127.0.0.1" in csp


async def test_post_body_forwarded(harness):
    proxy, session, mgr, client, port = harness
    payload = json.dumps({"text": "new task"}).encode()

    req_task = asyncio.create_task(
        client.post(
            f"http://127.0.0.1:{port}/api/todos",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
    )
    osc_text = await session.drain()
    upstream_header, upstream_body = _parse_osc(osc_text)
    assert upstream_header["method"] == "POST"
    assert json.loads(upstream_body) == {"text": "new task"}

    rid = upstream_header["request_id"]
    mgr.resolve_http(rid, {
        "status": 201,
        "headers": {"Content-Type": "application/json"},
    }, b"{}")
    resp = await asyncio.wait_for(req_task, timeout=3)
    assert resp.status == 201


async def test_no_active_session_returns_503(harness):
    proxy, session, mgr, client, port = harness
    proxy._get_session = lambda: None

    async with client.get(f"http://127.0.0.1:{port}/anything") as resp:
        assert resp.status == 503


async def test_gateway_timeout(harness):
    """If the remote never responds, the proxy returns 504 after timeout."""
    proxy, session, mgr, client, port = harness

    import iterm2_webgui.proxy_server as proxy_mod
    original = proxy_mod._HTTP_TIMEOUT
    proxy_mod._HTTP_TIMEOUT = 0.05
    try:
        async with client.get(f"http://127.0.0.1:{port}/slow") as resp:
            await session.drain()
            assert resp.status == 504
    finally:
        proxy_mod._HTTP_TIMEOUT = original
