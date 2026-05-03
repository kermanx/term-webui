"""WebGUI Bridge — CLI-side library.

Wraps the OSC 1337 tunnel protocol so application code never touches raw bytes.
Binary payloads (HTTP bodies, binary WebSocket frames) are base64-encoded
exactly once by the transport layer; there is no inner re-encoding.

Typical usage::

    from webgui_protocol.bridge import WebGUIBridge, Request, Response

    app = WebGUIBridge()

    @app.route("/")
    def index(req: Request) -> Response:
        return Response.html("<h1>Hello</h1>")

    @app.route("/api/items/<int:id>", methods=["DELETE"])
    def delete(req: Request, id: int) -> Response:
        ...
        return Response.json({"ok": True})

    @app.on_ws_open
    def on_open(conn_id: str, path: str) -> None:
        app.ws_send(conn_id, {"type": "welcome"})

    @app.on_ws_message
    def on_message(conn_id: str, data: str | bytes) -> None:
        app.ws_send(conn_id, {"echo": data if isinstance(data, str) else "<binary>"})

    @app.on_ws_close
    def on_close(conn_id: str) -> None:
        pass

    @app.background
    async def ticker() -> None:
        while True:
            await asyncio.sleep(2)
            for cid in app.ws_connections():
                app.ws_send(cid, {"type": "tick"})

    @app.route("/api/exit", methods=["POST"])
    def exit_route(req: Request) -> Response:
        app.exit_webview()
        return Response.json({"ok": True})

    if __name__ == "__main__":
        app.run()
"""
import asyncio
import base64
import json
import os
import re
import signal
import sys
import termios
import tty
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from webgui_protocol.osc import IDENTITY, CHUNK_SIZE, encode_osc, make_chunks

# ---------------------------------------------------------------------------
# OSC transport helpers (bytes layer for stdout)
# ---------------------------------------------------------------------------

_OSC_START = b"\x1b]1337;Custom=id=" + IDENTITY.encode() + b":"
_OSC_END = b"\x07"
_SEP = b"."   # separates base64(header) from base64(body); not in base64 alphabet


def _emit(header: dict, body: bytes | None = None) -> None:
    if body and len(body) > CHUNK_SIZE:
        for ch, cb in make_chunks(header, body, uuid.uuid4().hex):
            sys.stdout.buffer.write(encode_osc(ch, cb).encode())
    else:
        sys.stdout.buffer.write(encode_osc(header, body if body else None).encode())
    sys.stdout.buffer.flush()


# ---------------------------------------------------------------------------
# Stdin scanner
# ---------------------------------------------------------------------------


class _StdinScanner:
    """Scan raw stdin bytes and yield decoded ``(header, body)`` pairs."""

    def __init__(self) -> None:
        self._buf = bytearray()
        # msg_id → {"header": dict|None, "parts": {seq: bytes}, "total": int}
        self._chunks: dict[str, dict] = {}

    def feed(self, data: bytes) -> list[tuple[dict, bytes | None]]:
        self._buf.extend(data)
        results: list[tuple[dict, bytes | None]] = []
        while True:
            start = self._buf.find(_OSC_START)
            if start == -1:
                keep = len(_OSC_START) - 1
                if len(self._buf) > keep:
                    del self._buf[: len(self._buf) - keep]
                break
            if start > 0:
                del self._buf[:start]
            end = self._buf.find(_OSC_END, len(_OSC_START))
            if end == -1:
                break
            payload = bytes(self._buf[len(_OSC_START) : end])
            del self._buf[: end + len(_OSC_END)]

            parts = payload.split(_SEP, 1)
            try:
                header = json.loads(base64.b64decode(parts[0]).decode())
            except Exception:
                continue
            body: bytes | None = None
            if len(parts) > 1:
                try:
                    body = base64.b64decode(parts[1])
                except Exception:
                    continue

            if "_chunk" in header or header.get("type") == "chunk":
                assembled = self._reassemble(header, body)
                if assembled:
                    results.append(assembled)
            else:
                results.append((header, body))

        return results

    def _reassemble(
        self, header: dict, body: bytes | None
    ) -> tuple[dict, bytes | None] | None:
        chunk_meta = header.get("_chunk")
        if chunk_meta:
            msg_id = chunk_meta["msg_id"]
            total = chunk_meta["total"]
            original = {k: v for k, v in header.items() if k != "_chunk"}
            entry = self._chunks.setdefault(msg_id, {
                "header": original, "parts": {}, "total": total
            })
            entry["parts"][0] = body or b""
        elif header.get("type") == "chunk":
            msg_id = header["msg_id"]
            if msg_id not in self._chunks:
                self._chunks[msg_id] = {
                    "header": None, "parts": {}, "total": header["total"]
                }
            entry = self._chunks[msg_id]
            entry["parts"][header["seq"]] = body or b""
        else:
            return None

        if entry.get("header") is not None and len(entry["parts"]) == entry["total"]:
            full = b"".join(entry["parts"][i] for i in range(entry["total"]))
            del self._chunks[msg_id]
            return entry["header"], full or None
        return None


# ---------------------------------------------------------------------------
# Request / Response
# ---------------------------------------------------------------------------


class Request:
    """An incoming HTTP request forwarded from the WebView."""

    def __init__(self, header: dict, body: bytes | None) -> None:
        self.method: str = header.get("method", "GET").upper()
        parsed = urlparse(header.get("path", "/"))
        self.path: str = parsed.path
        self.path_qs: str = header.get("path", "/")
        self.query_string: str = parsed.query
        self.headers: dict[str, str] = header.get("headers", {})
        self.body: bytes = body or b""

    def json(self) -> Any:
        return json.loads(self.body.decode())

    def text(self) -> str:
        return self.body.decode()


@dataclass
class Response:
    """An HTTP response to send back to the WebView."""

    status: int = 200
    headers: dict = field(default_factory=dict)
    body: bytes = b""

    @staticmethod
    def json(data: Any, status: int = 200) -> "Response":
        return Response(
            status=status,
            headers={"Content-Type": "application/json; charset=utf-8"},
            body=json.dumps(data, ensure_ascii=False).encode(),
        )

    @staticmethod
    def html(text: str, status: int = 200) -> "Response":
        return Response(
            status=status,
            headers={"Content-Type": "text/html; charset=utf-8"},
            body=text.encode(),
        )

    @staticmethod
    def text(text: str, status: int = 200) -> "Response":
        return Response(
            status=status,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            body=text.encode(),
        )

    @staticmethod
    def bytes(data: bytes, content_type: str, status: int = 200) -> "Response":
        return Response(
            status=status,
            headers={"Content-Type": content_type},
            body=data,
        )

    def _header_dict(self, request_id: str) -> dict:
        return {
            "type": "http_response",
            "request_id": request_id,
            "status": self.status,
            "headers": self.headers,
        }


# ---------------------------------------------------------------------------
# Route pattern compiler
# ---------------------------------------------------------------------------


def _compile_pattern(path: str) -> tuple[re.Pattern, dict[str, type]]:
    """Compile ``/path/<type:name>`` patterns to a regex + type converter map."""
    converters: dict[str, type] = {}
    parts = re.split(r"(<(?:[a-z]+:)?[a-zA-Z_]\w*>)", path)
    regex = "^"
    for part in parts:
        m = re.fullmatch(r"<(?:([a-z]+):)?([a-zA-Z_]\w*)>", part)
        if m:
            typ_s, name = m.group(1), m.group(2)
            converters[name] = int if typ_s == "int" else str
            regex += rf"(?P<{name}>\d+)" if typ_s == "int" else rf"(?P<{name}>[^/]+)"
        else:
            regex += re.escape(part)
    regex += "$"
    return re.compile(regex), converters


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class WebGUIBridge:
    """Manages the OSC bridge event loop and routes requests to handlers."""

    def __init__(self) -> None:
        self._routes: list[tuple[set[str], re.Pattern, dict, Callable]] = []
        self._ws_open_cb: Callable | None = None
        self._ws_message_cb: Callable | None = None
        self._ws_close_cb: Callable | None = None
        self._bg_tasks: list[Callable] = []
        self._ws_conns: set[str] = set()
        self._should_exit = False
        self._got_first_http = False

    # ── Decorators ───────────────────────────────────────────────────────

    def route(self, path: str, methods: list[str] | None = None) -> Callable:
        """Register an HTTP route handler."""
        allowed = {m.upper() for m in (methods or ["GET"])}
        pattern, converters = _compile_pattern(path)

        def decorator(fn: Callable) -> Callable:
            self._routes.append((allowed, pattern, converters, fn))
            return fn

        return decorator

    def on_ws_open(self, fn: Callable) -> Callable:
        """Register a WebSocket open handler: ``fn(conn_id: str, path: str)``."""
        self._ws_open_cb = fn
        return fn

    def on_ws_message(self, fn: Callable) -> Callable:
        """Register a WebSocket message handler: ``fn(conn_id: str, data: str | bytes)``."""
        self._ws_message_cb = fn
        return fn

    def on_ws_close(self, fn: Callable) -> Callable:
        """Register a WebSocket close handler: ``fn(conn_id: str)``."""
        self._ws_close_cb = fn
        return fn

    def background(self, fn: Callable) -> Callable:
        """Register a background async task to start when ``run()`` is called."""
        self._bg_tasks.append(fn)
        return fn

    # ── Runtime API ──────────────────────────────────────────────────────

    def ws_send(self, conn_id: str, data: str | dict | bytes) -> None:
        """Send a WebSocket frame to the WebView client identified by *conn_id*."""
        if isinstance(data, dict):
            _emit({"type": "ws_frame", "conn_id": conn_id,
                   "text": json.dumps(data, ensure_ascii=False)})
        elif isinstance(data, bytes):
            _emit({"type": "ws_frame", "conn_id": conn_id}, data)
        else:
            _emit({"type": "ws_frame", "conn_id": conn_id, "text": str(data)})

    def ws_connections(self) -> set[str]:
        """Return a snapshot of currently open WebSocket connection IDs."""
        return set(self._ws_conns)

    def exit_webview(self) -> None:
        """Signal the plugin to close the WebView and restore the terminal tab."""
        _emit({"type": "exit_webview"})
        self._should_exit = True

    # ── Internal helpers ─────────────────────────────────────────────────

    async def _dispatch_http(self, request: Request) -> Response:
        for allowed, pattern, converters, handler in self._routes:
            if request.method not in allowed:
                continue
            m = pattern.match(request.path)
            if m:
                kwargs = {k: converters[k](v) for k, v in m.groupdict().items()}
                if asyncio.iscoroutinefunction(handler):
                    return await handler(request, **kwargs)
                return handler(request, **kwargs)
        return Response.text("Not Found", status=404)

    async def _call(self, fn: Callable, *args: Any) -> None:
        if asyncio.iscoroutinefunction(fn):
            await fn(*args)
        else:
            fn(*args)

    async def _reannounce(self) -> None:
        """Re-send init every 4 s until the first HTTP request arrives."""
        while not self._got_first_http and not self._should_exit:
            await asyncio.sleep(4)
            if self._got_first_http or self._should_exit:
                break
            _emit({"type": "init"})

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        scanner = _StdinScanner()

        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader),
            sys.stdin.buffer,
        )

        # connect_read_pipe sets O_NONBLOCK on stdin's fd which is shared with
        # stdout/stderr on the same PTY slave — large writes would fail with
        # EAGAIN.  Restore blocking mode on the output descriptors.
        import fcntl
        for fd in (sys.stdout.fileno(), sys.stderr.fileno()):
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl & ~os.O_NONBLOCK)

        _emit({"type": "init"})

        for bg in self._bg_tasks:
            asyncio.create_task(bg())
        asyncio.create_task(self._reannounce())

        while True:
            try:
                chunk = await reader.read(4096)
            except asyncio.CancelledError:
                break
            if not chunk:
                break

            for header, body in scanner.feed(chunk):
                mtype = header.get("type")

                if mtype == "http_request":
                    if not self._got_first_http:
                        self._got_first_http = True
                    req = Request(header, body)
                    resp = await self._dispatch_http(req)
                    _emit(resp._header_dict(header.get("request_id", "")),
                          resp.body if resp.body else None)
                    if self._should_exit:
                        return

                elif mtype == "ws_open":
                    conn_id = header["conn_id"]
                    self._ws_conns.add(conn_id)
                    if self._ws_open_cb:
                        await self._call(self._ws_open_cb, conn_id, header.get("path", "/"))
                    if self._should_exit:
                        return

                elif mtype == "ws_frame":
                    conn_id = header["conn_id"]
                    if conn_id in self._ws_conns and self._ws_message_cb:
                        payload: str | bytes = (
                            body if body is not None else header.get("text", "")
                        )
                        await self._call(self._ws_message_cb, conn_id, payload)
                    if self._should_exit:
                        return

                elif mtype == "ws_close":
                    conn_id = header["conn_id"]
                    self._ws_conns.discard(conn_id)
                    if self._ws_close_cb:
                        await self._call(self._ws_close_cb, conn_id)
                    if self._should_exit:
                        return

    def run(self) -> None:
        """Start the bridge event loop.  Blocks until ``exit_webview()`` is called."""
        fd = sys.stdin.fileno()
        is_tty = os.isatty(fd)
        old_attrs = None

        if is_tty:
            old_attrs = termios.tcgetattr(fd)
            tty.setcbreak(fd)

        def _restore(*_: Any) -> None:
            if is_tty and old_attrs is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)

        signal.signal(signal.SIGINT,  lambda *_: (_restore(), sys.exit(0)))
        signal.signal(signal.SIGTERM, lambda *_: (_restore(), sys.exit(0)))

        try:
            asyncio.run(self._run())
        finally:
            _restore()
