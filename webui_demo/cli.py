#!/usr/bin/env python3
"""Demo remote CLI: TODO list + WebSocket echo, served over the WebView Bridge.

Run inside an iTerm2 terminal window after the plugin is loaded:

    cd /path/to/iterm2-plugin-gui/webui_demo
    uv run cli.py
"""
import asyncio
import datetime
import json
from dataclasses import dataclass
from pathlib import Path

from webui_protocol.bridge import WebUIBridge, Request, Response

app = WebUIBridge()
ASSETS_DIR = Path(__file__).parent / "assets"

# ---------------------------------------------------------------------------
# TODO state
# ---------------------------------------------------------------------------

@dataclass
class Todo:
    id:   int
    text: str
    done: bool = False

_todos: list[Todo] = [
    Todo(1, "阅读 iTerm2 Python API 文档"),
    Todo(2, "实现 OSC 分块传输协议", done=True),
    Todo(3, "测试 WebSocket 双向通信"),
]
_next_id = 4

def _todos_json() -> list[dict]:
    return [{"id": t.id, "text": t.text, "done": t.done} for t in _todos]

# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

@app.route("/")
@app.route("/index.html")
def index(req: Request) -> Response:
    return Response.html(_HTML)

@app.route("/assets/todo_icon.svg")
def asset_icon(req: Request) -> Response:
    try:
        return Response.bytes((ASSETS_DIR / "todo_icon.svg").read_bytes(), "image/svg+xml")
    except OSError:
        return Response.text("Not Found", status=404)

@app.route("/api/todos")
def get_todos(req: Request) -> Response:
    return Response.json({"todos": _todos_json()})

@app.route("/api/todos", methods=["POST"])
def add_todo(req: Request) -> Response:
    global _next_id
    try:
        text = req.json().get("text", "").strip()
    except Exception:
        return Response.json({"error": "bad request"}, status=400)
    if not text:
        return Response.json({"error": "text required"}, status=400)
    _todos.append(Todo(_next_id, text))
    _next_id += 1
    return Response.json({"todos": _todos_json()})

@app.route("/api/todos/<int:id>", methods=["PATCH"])
def toggle_todo(req: Request, id: int) -> Response:
    for t in _todos:
        if t.id == id:
            t.done = not t.done
            return Response.json({"todos": _todos_json()})
    return Response.json({"error": "not found"}, status=404)

@app.route("/api/todos/<int:id>", methods=["DELETE"])
def delete_todo(req: Request, id: int) -> Response:
    before = len(_todos)
    _todos[:] = [t for t in _todos if t.id != id]
    if len(_todos) == before:
        return Response.json({"error": "not found"}, status=404)
    return Response.json({"todos": _todos_json()})

@app.route("/api/exit", methods=["POST"])
def exit_route(req: Request) -> Response:
    app.exit_webview()
    return Response.json({"ok": True})

# ---------------------------------------------------------------------------
# WebSocket handlers
# ---------------------------------------------------------------------------

_ws_msg_count: dict[str, int] = {}

@app.on_ws_open
def on_ws_open(conn_id: str, path: str) -> None:
    app.ws_send(conn_id, {
        "type": "welcome",
        "msg":  "WebSocket 已连接到远端 CLI！",
        "time": datetime.datetime.now().strftime("%H:%M:%S"),
    })

@app.on_ws_message
def on_ws_message(conn_id: str, data: str | bytes) -> None:
    if isinstance(data, bytes):
        return
    try:
        payload = json.loads(data)
    except Exception:
        payload = {"raw": data}
    _ws_msg_count[conn_id] = _ws_msg_count.get(conn_id, 0) + 1
    app.ws_send(conn_id, {
        "type":     "echo",
        "original": payload,
        "n":        _ws_msg_count[conn_id],
        "time":     datetime.datetime.now().strftime("%H:%M:%S"),
    })

@app.on_ws_close
def on_ws_close(conn_id: str) -> None:
    _ws_msg_count.pop(conn_id, None)

# ---------------------------------------------------------------------------
# Background: push a tick to every open WS connection every 2 seconds
# ---------------------------------------------------------------------------

@app.background
async def ws_ticker() -> None:
    n = 0
    while True:
        await asyncio.sleep(2)
        conns = app.ws_connections()
        if not conns:
            continue
        n += 1
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        for cid in conns:
            app.ws_send(cid, {"type": "tick", "n": n, "time": ts})

# ---------------------------------------------------------------------------
# HTML (TODO list + WebSocket test panel)
# ---------------------------------------------------------------------------

_HTML = """\
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TODO + WebSocket Demo</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0 }
  :root {
    --bg:    #0f172a;
    --card:  #1e293b;
    --border:#334155;
    --text:  #e2e8f0;
    --sub:   #94a3b8;
    --green: #4ade80;
    --blue:  #38bdf8;
    --amber: #fbbf24;
    --red:   #f87171;
    --r:     10px;
  }
  body { background:var(--bg); color:var(--text);
         font:13px/1.5 -apple-system,'Segoe UI',sans-serif;
         padding:14px; display:flex; flex-direction:column; gap:16px }
  /* ── shared card ── */
  .card { background:var(--card); border:1px solid var(--border);
          border-radius:var(--r); padding:14px; display:flex;
          flex-direction:column; gap:10px }
  .card-title { font-size:12px; font-weight:600; letter-spacing:.6px;
                text-transform:uppercase; color:var(--sub) }
  /* ── header ── */
  header { display:flex; align-items:center; gap:10px }
  header img { border-radius:10px; flex-shrink:0 }
  header h1  { font-size:16px; font-weight:700 }
  header p   { font-size:11px; color:var(--sub) }
  /* ── input row ── */
  .row { display:flex; gap:6px }
  input { flex:1; background:#0f172a; border:1px solid var(--border);
          border-radius:8px; color:var(--text); font-size:12px;
          padding:7px 10px; outline:none; transition:border-color .15s }
  input::placeholder { color:var(--sub) }
  input:focus { border-color:var(--blue) }
  /* ── buttons ── */
  button { border:none; border-radius:8px; cursor:pointer;
           font-size:12px; padding:7px 12px; transition:opacity .15s }
  button:hover { opacity:.85 }
  .btn-blue  { background:var(--blue);  color:#0f172a; font-weight:700 }
  .btn-green { background:var(--green); color:#0f172a; font-weight:700 }
  .btn-red   { background:var(--red);   color:#0f172a }
  .btn-ghost { background:transparent; color:var(--sub);
               border:1px solid var(--border); padding:5px 8px }
  /* ── stats ── */
  .stats { font-size:11px; color:var(--sub) }
  .stats span { color:var(--green); font-weight:600 }
  /* ── todo list ── */
  ul { list-style:none; display:flex; flex-direction:column; gap:6px }
  li { background:#0f172a; border:1px solid var(--border);
       border-radius:8px; display:flex; align-items:center;
       gap:8px; padding:8px 10px; transition:opacity .2s }
  li.done { opacity:.45 }
  li.done .task { text-decoration:line-through; color:var(--sub) }
  .check { width:18px; height:18px; border-radius:50%; flex-shrink:0;
           border:2px solid var(--border); background:none; cursor:pointer;
           display:flex; align-items:center; justify-content:center;
           font-size:10px; transition:all .15s; padding:0 }
  .done .check { background:var(--green); border-color:var(--green); color:#0f172a }
  .task { flex:1; font-size:12px }
  .del  { background:none; border:none; color:var(--border); cursor:pointer;
          font-size:14px; line-height:1; padding:2px 4px; transition:color .15s }
  .del:hover { color:var(--red) }
  .empty { text-align:center; color:var(--sub); padding:20px 0; font-size:12px }
  /* ── ws panel ── */
  .ws-status { display:inline-flex; align-items:center; gap:6px;
               font-size:11px; color:var(--sub) }
  .ws-status::before { content:''; width:7px; height:7px; border-radius:50%;
                       background:var(--border); flex-shrink:0 }
  .ws-status.on::before  { background:var(--green) }
  .ws-status.on { color:var(--green) }
  .ws-log { background:#0f172a; border:1px solid var(--border);
            border-radius:8px; height:150px; overflow-y:auto;
            padding:8px 10px; font-size:11px; font-family:monospace;
            display:flex; flex-direction:column; gap:3px }
  .ws-log:empty::after { content:'等待连接…'; color:var(--sub) }
  .le { padding:1px 0 }
  .le-sys  { color:var(--sub) }
  .le-recv { color:var(--blue) }
  .le-send { color:var(--amber) }
  .le-tick { color:#64748b }
  .le-err  { color:var(--red) }
  .tick-badge { display:inline-block; background:#1e293b;
                border:1px solid var(--border); border-radius:6px;
                font-size:11px; padding:1px 8px; color:var(--sub) }
</style>
</head>
<body>

<!-- ── Header ── -->
<header>
  <img src="/assets/todo_icon.svg" width="40" height="40" alt="todo">
  <div style="flex:1">
    <h1>WebUI Bridge Demo</h1>
    <p>via iTerm2 OSC tunnel</p>
  </div>
  <button class="btn-red" id="exit-btn">退出</button>
</header>

<!-- ── TODO card ── -->
<div class="card">
  <div class="card-title">TODO List</div>
  <div class="row">
    <input id="inp" placeholder="新增任务…" autocomplete="off">
    <button class="btn-blue" id="add-btn">+</button>
  </div>
  <div class="stats" id="stats"></div>
  <ul id="list"></ul>
</div>

<!-- ── WebSocket card ── -->
<div class="card">
  <div style="display:flex;align-items:center;justify-content:space-between">
    <div class="card-title">WebSocket 双向测试</div>
    <span class="ws-status" id="ws-status">未连接</span>
  </div>

  <div class="ws-log" id="ws-log"></div>

  <div class="row">
    <input id="ws-inp" placeholder="发送消息到 CLI…" disabled autocomplete="off">
    <button class="btn-green" id="ws-send" disabled>发送</button>
    <button class="btn-ghost" id="ws-toggle">连接</button>
    <button class="btn-ghost" id="ws-ping" title="直连代理测试，不经过 CLI">直连测试</button>
  </div>
  <div style="font-size:11px;color:var(--sub)">
    连接后 CLI 每 2 秒推送 tick。若连接失败，先点「直连测试」排查。
  </div>
</div>

<script>
// ── TODO ─────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = {method, headers:{}};
  if (body !== undefined) { opts.headers['Content-Type']='application/json'; opts.body=JSON.stringify(body); }
  return (await fetch(path, opts)).json();
}
function esc(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function render(todos) {
  const done = todos.filter(t=>t.done).length;
  document.getElementById('stats').innerHTML =
    `共 ${todos.length} 项，完成 <span>${done}</span> 项`;
  document.getElementById('list').innerHTML = todos.length
    ? todos.map(t=>`<li class="${t.done?'done':''}" data-id="${t.id}">
        <button class="check" onclick="toggle(${t.id})">${t.done?'✓':''}</button>
        <span class="task">${esc(t.text)}</span>
        <button class="del" onclick="del(${t.id})" title="删除">×</button>
      </li>`).join('')
    : '<li class="empty" style="border:none;background:none">暂无任务 ✨</li>';
}
async function load()       { render((await api('GET','/api/todos')).todos) }
async function toggle(id)   { render((await api('PATCH',`/api/todos/${id}`)).todos) }
async function del(id)      { render((await api('DELETE',`/api/todos/${id}`)).todos) }
async function addTodo() {
  const inp=document.getElementById('inp'), text=inp.value.trim();
  if (!text) return;
  inp.value='';
  render((await api('POST','/api/todos',{text})).todos);
  inp.focus();
}
document.getElementById('add-btn').addEventListener('click', addTodo);
document.getElementById('inp').addEventListener('keydown', e=>{ if(e.key==='Enter') addTodo(); });
load();

// ── WebSocket ─────────────────────────────────────────────────────────
let ws=null, tickCount=0;
const $log    = document.getElementById('ws-log');
const $status = document.getElementById('ws-status');
const $wsInp  = document.getElementById('ws-inp');
const $wsSend = document.getElementById('ws-send');
const $wsTog  = document.getElementById('ws-toggle');

function addLog(cls, text) {
  const d=document.createElement('div');
  d.className=`le le-${cls}`;
  const t=new Date().toLocaleTimeString('zh',{hour12:false});
  d.textContent=`${t}  ${text}`;
  $log.appendChild(d);
  $log.scrollTop=$log.scrollHeight;
}

function setConnected(on) {
  $status.textContent = on ? '已连接' : '未连接';
  $status.className   = on ? 'ws-status on' : 'ws-status';
  $wsInp.disabled  = !on;
  $wsSend.disabled = !on;
  $wsTog.textContent = on ? '断开' : '连接';
}

function openWs(url, onMsg) {
  addLog('sys', `⟳ host=${location.host}  proto=${location.protocol}  ws_api=${typeof WebSocket}`);
  if (typeof WebSocket === 'undefined') {
    addLog('err', '✗ WebSocket API 不可用 (WKWebView 限制)'); return null;
  }
  addLog('sys', `⟳ 连接 ${url}`);
  let sock;
  try { sock = new WebSocket(url); }
  catch(e) { addLog('err', `✗ new WebSocket 抛出: ${e.name}: ${e.message}`); return null; }
  addLog('sys', `readyState=${sock.readyState} (0=CONNECTING,1=OPEN,3=CLOSED)`);

  const _t = setTimeout(() => {
    if (sock.readyState !== WebSocket.OPEN) {
      addLog('err', `✗ 超时 6s readyState=${sock.readyState}`);
      sock.close();
    }
  }, 6000);

  sock.onopen  = () => { clearTimeout(_t); setConnected(true); addLog('sys', '▶ 已连接'); };
  sock.onerror = (e) => addLog('err', `✗ onerror type=${e.type}`);
  sock.onclose = (e) => { ws=null; setConnected(false); addLog('sys', `■ 关闭 code=${e.code} wasClean=${e.wasClean}`); };
  sock.onmessage = onMsg;
  return sock;
}

function wsToggle() {
  if (ws) { ws.close(); return; }
  ws = openWs(`ws://${location.host}/ws/echo`, ({data}) => {
    let p; try { p=JSON.parse(data); } catch { p={raw:data}; }
    if (p.type==='tick')    { tickCount++; addLog('tick',`⏱ tick #${p.n}  ${p.time}`); }
    else if (p.type==='welcome') addLog('recv',`✦ ${p.msg}`);
    else if (p.type==='echo')    addLog('recv',`← echo #${p.n}: ${JSON.stringify(p.original)}`);
    else                         addLog('recv',`← ${data}`);
  });
}

function wsPingTest() {
  addLog('sys', '── 直连测试 /_ws_ping (不经过 CLI) ──');
  const s = openWs(`ws://${location.host}/_ws_ping`, ({data}) => {
    addLog('recv', `← ping echo: ${data}`);
    s.close();
  });
  if (s) { s.onopen = (orig => () => { orig(); s.send('hello from browser'); })(s.onopen); }
}

function wsSend() {
  if (!ws || !$wsInp.value.trim()) return;
  const text = $wsInp.value.trim();
  ws.send(JSON.stringify({msg: text, time: new Date().toLocaleTimeString('zh',{hour12:false})}));
  addLog('send', `→ ${text}`);
  $wsInp.value='';
  $wsInp.focus();
}

$wsTog.addEventListener('click', wsToggle);
$wsSend.addEventListener('click', wsSend);
$wsInp.addEventListener('keydown', e=>{ if(e.key==='Enter') wsSend(); });
document.getElementById('ws-ping').addEventListener('click', wsPingTest);

// ── Exit ──────────────────────────────────────────────────────────────
document.getElementById('exit-btn').addEventListener('click', async () => {
  const btn = document.getElementById('exit-btn');
  btn.disabled = true;
  btn.textContent = '正在退出…';
  try { await fetch('/api/exit', {method: 'POST'}); } catch(_) {}
});
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run()
