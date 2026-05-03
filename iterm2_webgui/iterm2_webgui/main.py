"""iTerm2 AutoLaunch plugin: Terminal-Embedded WebView Bridge.

Intercepts OSC 1337 custom sequences from remote CLIs and manages a
per-session aiohttp proxy.  Each terminal tab that enters WebView mode gets
its own isolated port; the port is allocated on entry and released on exit.
A tab may enter and exit WebView mode repeatedly.

Remote CLI protocol (stdout → iTerm2):
    ESC ] 1337 ; Custom=id=webgui-bridge:<base64(header-json)>[.<base64(body)>] BEL

Local proxy → remote CLI protocol (stdin injection via async_send_text):
    Same OSC encoding — the remote process reads raw stdin.

Session lifecycle:
  - CLI sends `init`  → session registered; if not already in WebView mode,
                         a dedicated proxy is started and a browser tab opened.
  - CLI sends `exit_webview` → proxy stopped, browser tab closed, CLI unburied.
  - Tab closed by user → proxy stopped and state cleaned up immediately.
  - A session may re-enter WebView mode after exit (new proxy, new port).
"""
import asyncio
import logging
import re

import iterm2
import iterm2.notifications

from webgui_protocol.osc import IDENTITY, ChunkReassembler, decode_osc_payload
from .proxy_server import ProxyServer
from .session_mgr import SessionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_reassembler = ChunkReassembler()

# session_id → iterm2.Session for every tab that has an active CLI process.
_init_sessions: dict[str, "iterm2.Session"] = {}

# Per-session WebView state.  Created on WebView entry, destroyed on exit.
# Each value: {"proxy": ProxyServer, "mgr": SessionManager, "browser_tab": Tab|None}
_webview_states: dict[str, dict] = {}


async def _send_osc(session: "iterm2.Session", text: str) -> None:
    await session.async_send_text(text, suppress_broadcast=True)


# ---------------------------------------------------------------------------
# WebView entry / exit helpers
# ---------------------------------------------------------------------------


async def _enter_webview(
    session: "iterm2.Session", session_id: str, app: "iterm2.App"
) -> None:
    """Start a dedicated proxy and open a browser tab for *session_id*."""
    mgr = SessionManager()
    proxy = ProxyServer(mgr, lambda: _init_sessions.get(session_id), _send_osc)
    port = await proxy.start()
    url = f"http://127.0.0.1:{port}/"
    logger.info("Session %s: proxy started on port %d", session_id, port)

    _webview_states[session_id] = {"proxy": proxy, "mgr": mgr, "browser_tab": None}

    tab = session.tab
    window = app.get_window_for_tab(tab.tab_id) if tab else app.current_window
    if window is None:
        logger.warning("Session %s: no window found, browser tab not opened", session_id)
        return

    tab_index = None
    if tab:
        try:
            tab_index = window.tabs.index(tab)
        except ValueError:
            pass

    lwop = iterm2.LocalWriteOnlyProfile()
    lwop._simple_set("Custom Command", "Browser")
    lwop._simple_set("Initial URL", url)
    lwop._simple_set("Browser Show Toolbar", False)
    new_tab = await window.async_create_tab(index=tab_index, profile_customizations=lwop)
    if new_tab:
        _webview_states[session_id]["browser_tab"] = new_tab
        logger.info("Session %s: browser tab opened", session_id)

    await session.async_set_buried(True)


async def _exit_webview(session_id: str) -> None:
    """Tear down WebView for *session_id*: unbury CLI, close browser tab, stop proxy."""
    state = _webview_states.pop(session_id, None)
    if state is None:
        return

    session = _init_sessions.get(session_id)
    if session:
        try:
            await session.async_set_buried(False)
            logger.info("Session %s: unburied", session_id)
            await session.async_activate()
        except Exception as exc:
            logger.warning("Session %s: unbury/activate failed: %s", session_id, exc)

    browser_tab = state.get("browser_tab")
    if browser_tab:
        try:
            await browser_tab.async_close(force=True)
            logger.info("Session %s: browser tab closed", session_id)
        except Exception as exc:
            logger.warning("Session %s: browser tab close failed: %s", session_id, exc)

    await state["proxy"].stop()
    logger.info("Session %s: proxy stopped", session_id)


# ---------------------------------------------------------------------------
# OSC dispatch
# ---------------------------------------------------------------------------


async def _dispatch(
    session_id: str, header: dict, body: bytes | None, app: "iterm2.App"
) -> None:
    ptype = header.get("type")

    # Reassemble chunked messages before dispatching.
    if "_chunk" in header or ptype == "chunk":
        result = _reassembler.feed(session_id, header, body)
        if result:
            assembled_header, assembled_body = result
            await _dispatch(session_id, assembled_header, assembled_body, app)
        return

    # Route protocol messages to the per-session manager.
    state = _webview_states.get(session_id)

    if ptype == "http_response":
        if state:
            state["mgr"].resolve_http(header["request_id"], header, body)

    elif ptype == "ws_frame":
        if state:
            conn_id = header.get("conn_id")
            q = state["mgr"].get_ws_queue(conn_id)
            if q:
                if "text" in header:
                    await q.put(header["text"])
                elif body is not None:
                    await q.put(body)

    elif ptype == "ws_close":
        if state:
            conn_id = header.get("conn_id")
            q = state["mgr"].get_ws_queue(conn_id)
            if q:
                await q.put(None)

    elif ptype == "init":
        session = app.get_session_by_id(session_id)
        if session is None:
            logger.warning(
                "init from session %s but get_session_by_id returned None; retrying…",
                session_id,
            )
            await asyncio.sleep(0.5)
            session = app.get_session_by_id(session_id)
        if session:
            _init_sessions[session_id] = session
            logger.info(
                "Session %s: registered (init); active_sessions=%d",
                session_id, len(_init_sessions),
            )
            if session_id not in _webview_states:
                asyncio.create_task(_enter_webview(session, session_id, app))
        else:
            logger.error(
                "Session %s: still not found after retry — WebView will not open",
                session_id,
            )

    elif ptype == "exit_webview":
        asyncio.create_task(_exit_webview(session_id))

    else:
        logger.debug("Unknown OSC type %r from %s", ptype, session_id)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


async def main(connection: iterm2.Connection) -> None:
    app = await iterm2.async_get_app(connection)

    # ── OSC subscription (all sessions) ─────────────────────────────────
    _payload_re = re.compile(r"^.*$", re.DOTALL)

    async def _osc_callback(_conn, notification) -> None:
        if notification.sender_identity != IDENTITY:
            logger.debug(
                "OSC ignored: identity=%r (want %r) session=%s",
                notification.sender_identity, IDENTITY, notification.session,
            )
            return
        if not _payload_re.search(notification.payload):
            return
        result = decode_osc_payload(notification.payload)
        if result is None:
            logger.warning("Malformed OSC from session %s", notification.session)
            return
        header, body = result
        asyncio.create_task(_dispatch(notification.session, header, body, app))

    await iterm2.notifications.async_subscribe_to_custom_escape_sequence_notification(
        connection, _osc_callback, session=None
    )

    # ── SessionTerminationMonitor: clean up when a tab is closed ────────
    async def _track_termination() -> None:
        async with iterm2.SessionTerminationMonitor(connection) as monitor:
            while True:
                dead_id = await monitor.async_get()
                if dead_id not in _init_sessions:
                    continue
                del _init_sessions[dead_id]
                logger.info(
                    "Session %s terminated; registered=%d", dead_id, len(_init_sessions)
                )

                dead_state = _webview_states.pop(dead_id, None)
                if dead_state:
                    cancelled = dead_state["mgr"].reject_all_for_session(
                        dead_id, ConnectionError("Remote CLI session terminated")
                    )
                    if cancelled:
                        logger.info(
                            "Cancelled %d pending request(s) for %s", cancelled, dead_id
                        )
                    await dead_state["proxy"].stop()

    asyncio.create_task(_track_termination())

    logger.info("Bridge running  identity=%s", IDENTITY)
    await asyncio.Event().wait()


iterm2.run_forever(main)
