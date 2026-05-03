"""Local aiohttp proxy: bridges the Toolbelt WebView to the remote CLI via OSC."""
import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiohttp import WSMsgType, web

from webgui_protocol.osc import CHUNK_SIZE, encode_osc, make_chunks
from .session_mgr import SessionManager

if TYPE_CHECKING:
    import iterm2

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 8.0

# Blocks the WebView from making requests to any origin outside localhost.
_CSP = (
    "default-src 'self' http://127.0.0.1:* ws://127.0.0.1:*; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self' http://127.0.0.1:* ws://127.0.0.1:*;"
)

_INIT_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>WebGUI Bridge</title>
  <style>
    body {
      margin: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      height: 100vh;
      background: #1a1a2e;
      color: #8892a4;
      font: 13px/1.6 ui-monospace, monospace;
    }
    .card {
      text-align: center;
      padding: 2em 3em;
      border: 1px solid #2a3558;
      border-radius: 8px;
    }
    .dot { animation: pulse 1.4s ease-in-out infinite; }
    @keyframes pulse { 0%,100%{opacity:.3} 50%{opacity:1} }
  </style>
</head>
<body>
  <div class="card">
    <p>Terminal WebGUI Bridge</p>
    <p class="dot">Waiting for remote CLI init&hellip;</p>
  </div>
  <script>
    async function poll() {
      try {
        const r = await fetch('/_ready');
        if (r.ok) { window.location.reload(); return; }
      } catch (_) {}
      setTimeout(poll, 800);
    }
    poll();
  </script>
</body>
</html>
"""


class ProxyServer:
    """HTTP + WebSocket reverse-proxy that forwards traffic over OSC stdin injection."""

    def __init__(
        self,
        session_mgr: SessionManager,
        get_active_session: Callable[[], "iterm2.Session | None"],
        send_osc: Callable[["iterm2.Session", str], Awaitable[None]],
    ) -> None:
        self._mgr = session_mgr
        self._get_session = get_active_session
        self._send_osc = send_osc
        self.port = 0
        self._runner: web.AppRunner | None = None

    async def start(self) -> int:
        app = web.Application()
        app.router.add_get("/_init", self._init_page)
        app.router.add_get("/_ready", self._ready)
        app.router.add_get("/_ws_ping", self._handle_ws_ping)
        app.router.add_get("/ws/{name:.*}", self._handle_ws)
        app.router.add_route("*", "/{path:.*}", self._dispatch)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        logger.info("Proxy server listening on 127.0.0.1:%d", self.port)
        return self.port

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    async def _ready(self, _request: web.Request) -> web.Response:
        if self._get_session() is not None:
            return web.Response(text="ok")
        return web.Response(status=503, text="waiting")

    async def _init_page(self, _request: web.Request) -> web.Response:
        return web.Response(
            content_type="text/html",
            headers={"Content-Security-Policy": _CSP},
            text=_INIT_HTML,
        )

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            logger.warning("WS upgrade reached _dispatch (route miss?): %s", request.path)
            return await self._handle_ws(request)
        return await self._handle_http(request)

    # ------------------------------------------------------------------
    # HTTP bridging
    # ------------------------------------------------------------------

    async def _handle_http(self, request: web.Request) -> web.StreamResponse:
        session = self._get_session()
        if session is None:
            if request.path in ("/", "/index.html"):
                return web.Response(
                    content_type="text/html",
                    headers={"Content-Security-Policy": _CSP},
                    text=_INIT_HTML,
                )
            return web.Response(status=503, text="No active terminal session")

        request_id = uuid.uuid4().hex
        req_body = await request.read()
        future = self._mgr.register_http(request_id, session.session_id)

        req_header = {
            "type": "http_request",
            "request_id": request_id,
            "method": request.method,
            "path": request.path_qs,
            "headers": {
                k: v for k, v in request.headers.items()
                if k.lower() not in ("host",)
            },
        }

        try:
            await self._forward(session, req_header, req_body if req_body else None)
            resp_header, resp_body = await asyncio.wait_for(future, timeout=_HTTP_TIMEOUT)
        except asyncio.TimeoutError:
            self._mgr.reject_http(request_id, TimeoutError("Remote CLI timed out"))
            return web.Response(status=504, text="Gateway timeout")
        except Exception as exc:
            return web.Response(status=502, text=f"Bridge error: {exc}")

        body_bytes = resp_body or b""

        _skip = {"content-length", "transfer-encoding", "content-security-policy"}
        stream = web.StreamResponse(status=resp_header.get("status", 200))
        for k, v in resp_header.get("headers", {}).items():
            if k.lower() not in _skip:
                stream.headers[k] = v
        stream.headers["Content-Security-Policy"] = _CSP
        stream.content_length = len(body_bytes)
        await stream.prepare(request)
        await stream.write(body_bytes)
        return stream

    # ------------------------------------------------------------------
    # WebSocket bridging
    # ------------------------------------------------------------------

    async def _handle_ws_ping(self, request: web.Request) -> web.WebSocketResponse:
        """Direct-echo WebSocket — no OSC tunnel, used to verify ws:// works at all."""
        ws = web.WebSocketResponse()
        try:
            await ws.prepare(request)
        except Exception:
            logger.exception("ws_ping prepare failed")
            raise
        logger.info("ws_ping open")
        await ws.send_str('{"type":"ping_welcome","msg":"direct echo ready"}')
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await ws.send_str(msg.data)
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
        logger.info("ws_ping close")
        return ws

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        try:
            await ws.prepare(request)
        except Exception:
            logger.exception("WebSocket prepare failed for %s", request.path)
            raise

        session = self._get_session()
        conn_id = uuid.uuid4().hex
        queue = self._mgr.register_ws(
            conn_id, session.session_id if session else ""
        )
        logger.info("WS open  conn=%s  path=%s  session=%s",
                    conn_id[:8], request.path_qs,
                    session.session_id[:8] if session else "none")

        if session:
            await self._forward(session, {
                "type": "ws_open",
                "conn_id": conn_id,
                "path": request.path_qs,
            })

        async def drain() -> None:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, bytes):
                    await ws.send_bytes(item)
                else:
                    await ws.send_str(item)

        drainer = asyncio.create_task(drain())
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT and session:
                    await self._forward(session, {
                        "type": "ws_frame",
                        "conn_id": conn_id,
                        "text": msg.data,
                    })
                elif msg.type == WSMsgType.BINARY and session:
                    await self._forward(session,
                                        {"type": "ws_frame", "conn_id": conn_id},
                                        msg.data)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            drainer.cancel()
            self._mgr.unregister_ws(conn_id)
            logger.info("WS close conn=%s", conn_id[:8])
            if session:
                await self._forward(session, {"type": "ws_close", "conn_id": conn_id})

        return ws

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _forward(
        self,
        session: "iterm2.Session",
        header: dict,
        body: bytes | None = None,
    ) -> None:
        """Send *header* (and optional *body*) upstream via OSC."""
        if body and len(body) > CHUNK_SIZE:
            for chunk_header, chunk_body in make_chunks(header, body):
                await self._send_osc(session, encode_osc(chunk_header, chunk_body))
        else:
            await self._send_osc(session, encode_osc(header, body if body else None))
