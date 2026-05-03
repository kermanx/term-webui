# term-webgui

Serve a web UI from a CLI process — displayed inside the terminal window, no browser, no ports.

## Background

Developers working in terminal environments face a persistent tradeoff:

**TUI (Terminal User Interface)**
- Renders inside the terminal — no window switching, travels naturally over SSH
- Character-grid constraints limit expressiveness; edge cases and rendering bugs are common

**Web GUI**
- Mature HTML/CSS/JS ecosystem; charts, animations, and complex interactions are straightforward
- Requires a local HTTP server and a manual browser open; SSH use requires port forwarding, which is fragile against firewalls and security groups, and risks port conflicts

### The insight

TUIs win on **transport** — no ports, no browser, the UI follows the terminal session. Web GUIs win on **expressiveness and developer velocity**. If a terminal emulator can host a browser view, a CLI process can deliver a full web UI *through the terminal itself*, getting both.

### The approach

Define a terminal extension protocol: the CLI exchanges HTTP and WebSocket data with the terminal emulator via standard OSC escape sequences written to stdout/stdin. The terminal emulator renders the interface in an embedded WebView and acts as a local HTTP proxy — invisible to the user.

```
┌─────────────┐  OSC (stdout)   ┌──────────────────┐  HTTP / WS   ┌──────────┐
│  CLI process│ ──────────────► │ Terminal emulator │ ◄──────────► │ WebView  │
│             │ ◄────────────── │ (local proxy)     │              │          │
└─────────────┘  OSC (stdin)    └──────────────────┘              └──────────┘
```

## Repository layout

```
webgui_protocol/   OSC codec and CLI-side HTTP/WebSocket bridge (pure Python, no terminal dependencies)
iterm2_webgui/     iTerm2 adapter — AutoLaunch plugin implementing the terminal side of the protocol
webgui_demo/       Demo CLI application
```

```bash
uv sync       # install all workspace dependencies
make test     # run all tests across subprojects
make dist     # build dist/iterm2-webgui.zip — importable via iTerm2 → Scripts → Import
```

## Protocol

### Wire format

Every message is an OSC 1337 custom sequence:

```
ESC ] 1337 ; Custom=id=webgui-bridge:<base64(header-json)>[.<base64(body)>] BEL
```

| Token           | Description                                           |
|-----------------|-------------------------------------------------------|
| `webgui-bridge` | Fixed identity string                                 |
| `header-json`   | Compact UTF-8 JSON (`separators=(",",":")`)           |
| `.`             | Separator — not in the base64 alphabet, unambiguous   |
| `body`          | Raw binary payload, omitted when there is nothing to send |

Binary payloads are base64-encoded exactly **once** by the transport layer and never re-encoded inside the JSON header.

### Direction

| Direction              | Mechanism                                              |
|------------------------|--------------------------------------------------------|
| CLI → terminal         | CLI writes raw OSC bytes to `sys.stdout.buffer`        |
| Terminal → CLI         | Terminal emulator injects the sequence into stdin      |

### Chunked transport

Payloads exceeding 16 384 bytes are split so each OSC sequence stays within TTY / SSH buffer limits. The header JSON is never split.

**First chunk** — carries the original header plus chunk metadata:
```
header: { …original fields…, "_chunk": {"msg_id":"<hex>","seq":0,"total":N} }
body:   first 16 384 bytes
```

**Continuation chunks:**
```
header: { "type": "chunk", "msg_id": "<hex>", "seq": i, "total": N }
body:   next 16 384 bytes
```

The `_chunk` key is stripped before dispatch; application code never sees chunk envelopes.

### Session lifecycle

```
CLI                                         Terminal emulator
 │                                             │
 │──── init ──────────────────────────────────►│  Proxy started; WebView opened
 │                                             │
 │◄─── http_request ───────────────────────────│  WebView makes an HTTP request
 │──── http_response ─────────────────────────►│  CLI responds
 │                                             │
 │◄─── ws_open ────────────────────────────────│  WebView opens a WebSocket
 │◄─── ws_frame ───────────────────────────────│  WebView → CLI frame
 │──── ws_frame ──────────────────────────────►│  CLI → WebView frame
 │◄─── ws_close ───────────────────────────────│  WebView closes WebSocket
 │                                             │
 │──── exit_webview ──────────────────────────►│  WebView and proxy torn down
```

- Send exactly one `init` per WebView session. Re-emit every 4 s until the first `http_request` arrives, in case the terminal was not yet ready.
- After `exit_webview`, the CLI may re-enter WebView mode by sending `init` again; each cycle gets a new proxy port.
- If the terminal tab is closed by the user, the terminal side cleans up without waiting for `exit_webview`.

### Message reference

#### CLI → terminal

**`init`** — register the session and request WebView creation.
```
header: { "type": "init" }
```

**`http_response`** — reply to an `http_request`.
```
header: { "type": "http_response", "request_id": "<hex>", "status": 200,
          "headers": { "Content-Type": "text/html; charset=utf-8" } }
body:   <raw response bytes>
```

**`ws_frame`** (text)
```
header: { "type": "ws_frame", "conn_id": "<hex>", "text": "..." }
```

**`ws_frame`** (binary)
```
header: { "type": "ws_frame", "conn_id": "<hex>" }
body:   <raw bytes>
```

**`ws_close`**
```
header: { "type": "ws_close", "conn_id": "<hex>" }
```

**`exit_webview`** — request teardown.
```
header: { "type": "exit_webview" }
```

#### Terminal → CLI

**`http_request`** — an HTTP request forwarded from the WebView.
```
header: { "type": "http_request", "request_id": "<hex>",
          "method": "POST", "path": "/api/items?q=foo",
          "headers": { "Content-Type": "application/json" } }
body:   <raw request bytes>   (omitted for GET etc.)
```

**`ws_open`** — a new WebSocket connection was opened.
```
header: { "type": "ws_open", "conn_id": "<hex>", "path": "/ws/echo" }
```

**`ws_frame`** and **`ws_close`** — same shape as CLI → terminal above.

### Constraints

| Constraint             | Value                                                        |
|------------------------|--------------------------------------------------------------|
| Header JSON            | Compact, no extra whitespace                                 |
| Chunking threshold     | Body > 16 384 bytes                                          |
| Re-announcement period | Every 4 s until first `http_request`                         |
| Identity string        | `webgui-bridge` (fixed, case-sensitive)                      |
| Sessions per tab       | At most one active WebView session per terminal tab          |
| `ws_frame` payload     | Exactly one of: `text` field (string) or body section (bytes)|
| Body separator         | `.` — not in the base64 alphabet (`A-Za-z0-9+/=`)           |
