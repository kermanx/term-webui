"""Request/connection state machine for the proxy bridge."""
import asyncio
import uuid


class SessionManager:
    """Tracks pending HTTP futures and live WebSocket queues.

    All session references are iterm2 session-ID strings; the actual
    iterm2.Session objects live only in main.py where the app reference is held.
    """

    def __init__(self) -> None:
        # request_id -> (Future, iterm2_session_id)
        self._http: dict[str, tuple[asyncio.Future, str]] = {}
        # conn_id -> asyncio.Queue (None sentinel signals close)
        self._ws_queues: dict[str, asyncio.Queue] = {}
        # conn_id -> iterm2_session_id
        self._ws_sessions: dict[str, str] = {}

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def new_request_id(self) -> str:
        return uuid.uuid4().hex

    def register_http(self, request_id: str, iterm2_session_id: str) -> asyncio.Future:
        """Reserve a Future for *request_id*; returns it to await on."""
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._http[request_id] = (future, iterm2_session_id)
        return future

    def get_http_session_id(self, request_id: str) -> str | None:
        entry = self._http.get(request_id)
        return entry[1] if entry else None

    def resolve_http(
        self, request_id: str, header: dict, body: bytes | None
    ) -> bool:
        """Resolve the Future for *request_id* with ``(header, body)``."""
        entry = self._http.pop(request_id, None)
        if entry and not entry[0].done():
            entry[0].set_result((header, body))
            return True
        return False

    def reject_http(self, request_id: str, exc: Exception) -> None:
        entry = self._http.pop(request_id, None)
        if entry and not entry[0].done():
            entry[0].set_exception(exc)

    def reject_all_for_session(self, iterm2_session_id: str, exc: Exception) -> int:
        """Fail every pending HTTP future belonging to *iterm2_session_id*.

        Called when a session terminates so the proxy returns 502 immediately
        rather than waiting for the gateway timeout.
        Returns the number of futures cancelled.
        """
        victims = [
            rid for rid, (_, sid) in self._http.items()
            if sid == iterm2_session_id
        ]
        for rid in victims:
            self.reject_http(rid, exc)
        return len(victims)

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    def new_conn_id(self) -> str:
        return uuid.uuid4().hex

    def register_ws(self, conn_id: str, iterm2_session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._ws_queues[conn_id] = q
        self._ws_sessions[conn_id] = iterm2_session_id
        return q

    def get_ws_queue(self, conn_id: str) -> asyncio.Queue | None:
        return self._ws_queues.get(conn_id)

    def get_ws_session_id(self, conn_id: str) -> str | None:
        return self._ws_sessions.get(conn_id)

    def unregister_ws(self, conn_id: str) -> None:
        self._ws_queues.pop(conn_id, None)
        self._ws_sessions.pop(conn_id, None)
