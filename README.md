# WebGUI Bridge Protocol

Terminal-embedded WebView bridge over iTerm2 OSC 1337 custom sequences.

## 1. Overview

The protocol lets a remote CLI process (running in an iTerm2 terminal) serve a web application to a WebView embedded in the same terminal window. All communication is tunneled through the terminal's stdin/stdout using OSC 1337 custom escape sequences.

```
┌─────────────┐  OSC (stdout)  ┌────────────────┐  HTTP / WS  ┌──────────────┐
│  Remote CLI │ ──────────────► │ iTerm2 Plugin  │ ◄──────────► │   WebView    │
│  (Python)   │ ◄────────────── │ (local proxy)  │              │ (WKWebView)  │
└─────────────┘  OSC (stdin)   └────────────────┘              └──────────────┘
```

The plugin listens for OSC sequences from the CLI, starts a per-session local HTTP proxy, opens a WebView to that proxy, and forwards HTTP and WebSocket traffic back and forth over the OSC tunnel.

---

## 2. Transport Layer

### Wire Format

Every message is an OSC 1337 custom sequence whose payload has two sections:

```
ESC ] 1337 ; Custom=id=webgui-bridge:<base64(header-json)>[.<base64(body)>] BEL
```

| Token           | Value / description                                     |
|-----------------|---------------------------------------------------------|
| `ESC`           | `0x1B`                                                  |
| `BEL`           | `0x07`                                                  |
| `webgui-bridge` | Fixed identity string (shared secret)                   |
| `header-json`   | Compact UTF-8 JSON containing all structured metadata   |
| `.`             | Separator — not in the base64 alphabet, unambiguous     |
| `body`          | Raw binary bytes, present only when there is a payload  |

**Key property:** binary payloads (HTTP bodies, binary WebSocket frames, chunk data)
are base64-encoded exactly **once** — as the `body` section of the OSC sequence.
They are never re-encoded inside the JSON header.  Text content (JSON data, URLs,
header strings) lives directly in the header JSON without any encoding overhead.

The body section is omitted entirely when there is no binary payload (empty body,
or messages that carry no data at all).

### Header JSON

Must be compact — no extra whitespace (`separators=(",", ":")`).
Binary fields (`body_b64`, `data_b64`) do **not** exist in this protocol.

### Direction

| Direction     | Mechanism                                                                  |
|---------------|----------------------------------------------------------------------------|
| CLI → Plugin  | CLI writes raw OSC bytes to `sys.stdout.buffer`                            |
| Plugin → CLI  | Plugin calls iTerm2 `async_send_text` to inject the sequence into stdin    |

---

## 3. Chunked Transport

Large binary payloads are split into slices so each OSC sequence stays within
typical TTY / SSH buffer limits.

**Threshold:** split when `len(body) > 16 384 bytes`.

The **header JSON is never split** — it is always small (< 1 KB).

### Chunk Envelopes

**First chunk** (carries the full original header plus chunk metadata):

```
header: { …original message fields…, "_chunk": {"msg_id":"<hex>","seq":0,"total":N} }
body:   <first 16 384 bytes of the payload>
```

**Continuation chunks** (carry only the sequence metadata):

```
header: { "type": "chunk", "msg_id": "<hex>", "seq": i, "total": N }
body:   <next 16 384 bytes of the payload>
```

The `"_chunk"` key in the first chunk header is stripped before the message is
dispatched to application code.

### Reassembly

The receiver collects body slices keyed by `(session_id, msg_id)` and dispatches
the full `(original_header, concatenated_body)` once all `total` chunks arrive.
Application code never sees chunk envelopes.

---

## 4. Session Lifecycle

```
CLI                                         Plugin
 │                                             │
 │──── init ──────────────────────────────────►│  Session registered; proxy started; WebView opened
 │                                             │
 │◄─── http_request ───────────────────────────│  WebView makes an HTTP request
 │──── http_response ─────────────────────────►│  CLI responds
 │                                             │
 │◄─── ws_open ────────────────────────────────│  WebView opens a WebSocket
 │◄─── ws_frame ───────────────────────────────│  WebView → CLI frame
 │──── ws_frame ──────────────────────────────►│  CLI → WebView frame (server push)
 │◄─── ws_close ───────────────────────────────│  WebView closes WebSocket
 │                                             │
 │──── exit_webview ──────────────────────────►│  CLI requests teardown
 │                                             │  Plugin closes WebView, stops proxy, restores tab
```

### Rules

- **One `init` per entry.** Send exactly one `init` when entering WebView mode.
- **Re-announcement.** If the plugin was not ready when `init` was first sent,
  re-emit `init` every **4 seconds** until the first `http_request` arrives.
- **Re-entry.** After `exit_webview` a tab may re-enter WebView mode by sending
  `init` again; each cycle gets a new proxy on a new port.
- **Tab close.** If the user closes the terminal tab, the plugin cleans up
  immediately without an `exit_webview` message.
- **Port isolation.** Each tab in WebView mode owns a unique OS-assigned proxy
  port, allocated on entry and released on exit.

---

## 5. Message Reference

### 5.1 CLI → Plugin

#### `init`

Registers the CLI session and triggers WebView creation.

```
header: { "type": "init" }
body:   (none)
```

#### `http_response`

Response to an `http_request`.

```
header: {
  "type":       "http_response",
  "request_id": "<hex>",
  "status":     200,
  "headers":    { "Content-Type": "text/html; charset=utf-8" }
}
body: <raw response bytes>   ← single base64 encoding by transport
```

| Field        | Type   | Description                                 |
|--------------|--------|---------------------------------------------|
| `request_id` | string | Echoes the `request_id` from `http_request` |
| `status`     | int    | HTTP status code                            |
| `headers`    | object | Response headers (`string → string`)        |

Body is omitted from the OSC sequence when empty.

#### `ws_frame` (text, CLI → WebView)

```
header: { "type": "ws_frame", "conn_id": "<hex>", "text": "..." }
body:   (none)
```

#### `ws_frame` (binary, CLI → WebView)

```
header: { "type": "ws_frame", "conn_id": "<hex>" }
body:   <raw binary bytes>   ← single base64 encoding by transport
```

#### `ws_close` (CLI → WebView)

```
header: { "type": "ws_close", "conn_id": "<hex>" }
body:   (none)
```

#### `exit_webview`

```
header: { "type": "exit_webview" }
body:   (none)
```

After emitting this, the CLI should exit its event loop. The plugin closes the
browser tab, stops the proxy, and restores the CLI terminal tab.

---

### 5.2 Plugin → CLI

#### `http_request`

An HTTP request forwarded from the WebView.

```
header: {
  "type":       "http_request",
  "request_id": "<hex>",
  "method":     "POST",
  "path":       "/api/todos?filter=active",
  "headers":    { "Content-Type": "application/json" }
}
body: <raw request bytes>   ← single base64 encoding by transport
```

| Field        | Type   | Description                                      |
|--------------|--------|--------------------------------------------------|
| `request_id` | string | Opaque ID; must be echoed in `http_response`     |
| `method`     | string | HTTP method (`GET`, `POST`, `PATCH`, `DELETE` …) |
| `path`       | string | Path + query string (e.g. `/api/items?q=foo`)    |
| `headers`    | object | Request headers (`string → string`)              |

Body is omitted from the OSC sequence when empty (e.g. GET requests).

#### `ws_open`

A new WebSocket connection was opened by the WebView.

```
header: { "type": "ws_open", "conn_id": "<hex>", "path": "/ws/echo" }
body:   (none)
```

#### `ws_frame` (text, WebView → CLI)

```
header: { "type": "ws_frame", "conn_id": "<hex>", "text": "..." }
body:   (none)
```

#### `ws_frame` (binary, WebView → CLI)

```
header: { "type": "ws_frame", "conn_id": "<hex>" }
body:   <raw binary bytes>   ← single base64 encoding by transport
```

#### `ws_close` (WebView → CLI)

```
header: { "type": "ws_close", "conn_id": "<hex>" }
body:   (none)
```

---

## 6. Encoding Comparison

| What is sent          | Old encoding          | New encoding                   |
|-----------------------|-----------------------|--------------------------------|
| HTTP body             | `base64(base64(body))` | `base64(body)` (transport only) |
| Binary WS frame       | `base64(base64(data))` | `base64(data)` (transport only) |
| Text WS frame         | `base64(json_with_text)` | `base64(header_json)` (same)  |
| Metadata (method …)   | inside outer base64   | inside outer base64 (same)     |

---

## 7. Constraints

| Constraint             | Value / rule                                                     |
|------------------------|------------------------------------------------------------------|
| Header JSON encoding   | Compact, no extra whitespace (`separators=(",", ":")`)           |
| Chunking threshold     | Split when body exceeds **16 384 bytes**                         |
| Re-announcement period | Every **4 seconds** until first `http_request` received          |
| Identity string        | `webgui-bridge` (fixed, case-sensitive)                          |
| Sessions per tab       | At most one active WebView session per terminal tab              |
| Concurrent init        | Do not send `init` while already in WebView mode                 |
| `ws_frame` payload     | Exactly one of `text` field (string) or body section (bytes)     |
| Body section separator | `.` — not in the standard base64 alphabet (`A-Za-z0-9+/=`)      |
