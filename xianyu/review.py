"""本地人工审核页：点按钮把「采纳 / 已联系 / 已成交 / 忽略」写回数据库。

只监听 127.0.0.1，不上传任何数据。
"""
from __future__ import annotations

import html
import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import Config
from .report import CSS, money, row_to_item
from .store import STATUS_LABELS, connect, rows_ignored, rows_pending, rows_suspicious, set_status

ACTIONS = [
    ("adopted", "采纳"),
    ("contacted", "已联系"),
    ("done", "已成交"),
    ("ignored", "忽略"),
]

REVIEW_CSS = """
header.top{padding:14px 20px 10px}
.top-row{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap}
.top-row h1{margin:0}
.stats{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:12.5px;align-items:center}
.stats b{font-size:16px;color:var(--ink);margin-right:3px}
.top-actions{display:flex;gap:8px;align-items:center}
.tabs{display:flex;gap:6px;margin-top:12px;overflow-x:auto;padding-bottom:2px;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab{border:0;background:transparent;padding:8px 14px;border-radius:10px;font-size:13.5px;
color:var(--muted);cursor:pointer;white-space:nowrap;font-weight:500}
.tab:hover{background:#f3f4f6;color:var(--ink)}
.tab.on{background:#111827;color:#fff}
.panel{display:none;margin-top:4px}
.panel.on{display:block}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(440px,1fr));gap:12px}
.card{position:relative;display:grid;grid-template-columns:96px minmax(0,1fr);gap:14px;
background:#fff;border:1px solid var(--line);border-left:4px solid #d1d5db;border-radius:14px;
padding:14px;transition:box-shadow .15s,transform .15s}
.card:hover{box-shadow:0 6px 22px rgba(17,24,39,.08)}
.card.vc-hot{border-left-color:#dc2626}
.card.vc-good{border-left-color:#16a34a}
.card.vc-ok{border-left-color:#0284c7}
.card.vc-meh{border-left-color:#9ca3af}
.card.vc-warn{border-left-color:#d97706}
.card.vc-skip{border-left-color:#e5e7eb}
.card.done{opacity:.45;pointer-events:none}
.thumb{position:relative;width:96px;height:96px;border-radius:10px;overflow:hidden;
background:#eef0f3;display:flex;align-items:center;justify-content:center}
.thumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.thumb .ph{display:none;color:#9ca3af;font-size:12px}
.thumb.empty-thumb .ph{display:block}
.body{min-width:0}
.head{display:flex;gap:12px;align-items:flex-start;justify-content:space-between}
.card .title{max-width:none;font-size:14px;line-height:1.45}
.head .price{font-size:18px;font-weight:800;white-space:nowrap;text-align:right;color:#111827}
.head .ref{display:block;font-size:11.5px;color:var(--muted);font-weight:400;text-align:right}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:7px;align-items:center}
.badges .v{margin:0}
.sb{display:inline-flex;align-items:baseline;gap:1px;background:#111827;color:#fff;
border-radius:8px;padding:2px 8px;font-weight:700;font-size:14px}
.sb small{font-size:10px;font-weight:500;opacity:.7}
.disc{color:var(--good);background:#f0fdf4;border-radius:999px;padding:2px 8px;
font-weight:700;font-size:12px}
.meta-line{color:var(--muted);font-size:12px;margin-top:5px;line-height:1.45}
.tag-list{margin-top:6px}
.reasons{margin-top:6px;display:grid;gap:2px}
.reason-line{font-size:12.5px;color:#374151}
.reason-line::before{content:"· ";color:#9ca3af}
.flags{margin-top:3px;display:grid;gap:2px}
.flag-line{color:var(--warn);font-size:12px}
.note-line{margin-top:7px;font-size:12px;color:#92400e;background:#fffbeb;
border:1px solid #fde68a;border-radius:8px;padding:5px 8px}
.acts{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.act{border:1px solid var(--line);background:#fff;border-radius:8px;padding:5px 11px;
font-size:12px;line-height:1.2;cursor:pointer;color:#374151;text-decoration:none}
.act:hover{filter:brightness(.97)}
.act:disabled{opacity:.5;cursor:default}
.act.adopted{background:#f0fdf4;border-color:#bbf7d0;color:#15803d}
.act.contacted{background:#eff6ff;border-color:#bfdbfe;color:#1d4ed8}
.act.done{background:#f9fafb;border-color:#e5e7eb;color:#4b5563}
.act.ignored{background:#fef2f2;border-color:#fecaca;color:#b91c1c}
.act.restore{background:#eff6ff;border-color:#bfdbfe;color:#1d4ed8}
.act.link{background:#fff;border-color:#c7d2fe;color:#1d4ed8}
.act:focus-visible,.tab:focus-visible,.chip:focus-visible{outline:2px solid #93c5fd;outline-offset:1px}
#toast{position:fixed;right:18px;bottom:18px;background:#111827;color:#fff;padding:9px 15px;
border-radius:9px;font-size:12.5px;opacity:0;transform:translateY(6px);
transition:.2s;pointer-events:none;z-index:20}
#toast.on{opacity:1;transform:translateY(0)}
@media (max-width:720px){
  header.top{padding:12px 14px 8px}
  main{padding:12px 12px 50px}
  .grid{grid-template-columns:1fr}
  .card{grid-template-columns:64px minmax(0,1fr);gap:10px;padding:12px;border-radius:12px}
  .thumb{width:64px;height:64px}
  .head{flex-direction:column;gap:3px}
  .head .price{text-align:left;font-size:17px}
  .head .ref{text-align:left}
  .tab{padding:7px 11px}
}
"""

REVIEW_JS = """
(function(){
  var TABS=['pending','ignored'];

  function toast(msg){
    var t=document.getElementById('toast');
    t.textContent=msg;t.classList.add('on');
    setTimeout(function(){t.classList.remove('on')},1600);
  }

  function currentTab(){
    var name=location.hash.replace('#','');
    return TABS.indexOf(name)>=0?name:'pending';
  }

  function activate(name){
    document.querySelectorAll('.tab').forEach(function(tab){
      tab.classList.toggle('on',tab.dataset.tab===name);
    });
    document.querySelectorAll('.panel').forEach(function(panel){
      panel.classList.toggle('on',panel.dataset.panel===name);
    });
  }

  document.querySelectorAll('.tab').forEach(function(tab){
    tab.addEventListener('click',function(){
      var name=tab.dataset.tab;
      try{history.replaceState(null,'','#'+name)}catch(e){location.hash=name}
      activate(name);
    });
  });
  window.addEventListener('hashchange',function(){activate(currentTab())});

  var refresh=document.getElementById('refresh');
  if(refresh)refresh.addEventListener('click',function(){location.reload()});

  document.addEventListener('click',function(e){
    var button=e.target.closest('.act[data-status]');
    if(!button)return;
    var card=button.closest('.card');
    button.disabled=true;
    fetch('/api/status',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({item_id:button.dataset.id,status:button.dataset.status})
    }).then(function(response){return response.json()})
      .then(function(data){
        if(data.ok){
          if(card)card.classList.add('done');
          toast(data.message||'已记录');
          setTimeout(function(){location.reload()},650);
        }else{
          toast(data.message||'失败');
          button.disabled=false;
        }
      })
      .catch(function(){
        toast('保存失败，检查终端是否还在运行');
        button.disabled=false;
      });
  });

  activate(currentTab());
})();
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _thumb(item: dict) -> str:
    image = (item.get("image") or "").strip()
    if not image:
        return '<div class="thumb empty-thumb"><span class="ph">无图</span></div>'
    return (
        '<div class="thumb">'
        f'<img src="{html.escape(image, quote=True)}" alt="" loading="lazy" '
        'referrerpolicy="no-referrer" '
        "onerror=\"this.style.display='none';this.parentElement.classList.add('empty-thumb')\">"
        '<span class="ph">无图</span></div>'
    )


def _card(item: dict, *, ignored: bool = False) -> str:
    esc = html.escape
    item_id = esc(item["item_id"], quote=True)
    if ignored:
        buttons = (
            f'<button class="act restore" data-id="{item_id}" '
            'data-status="pending">恢复待审核</button>'
        )
    else:
        buttons = "".join(
            f'<button class="act {status}" data-id="{item_id}" data-status="{status}">{label}</button>'
            for status, label in ACTIONS
        )
    url = esc(item["url"], quote=True)
    link = (
        f'<a class="act link" href="{url}" target="_blank" rel="noreferrer">打开闲鱼</a>'
    )
    reasons = "".join(
        f'<div class="reason-line">{esc(reason)}</div>' for reason in item["reasons"][:3]
    )
    flags = "".join(
        f'<div class="flag-line">{esc(flag)}</div>' for flag in item["flags"][:3]
    )
    tags = "".join(f'<span class="tag">{esc(tag)}</span>' for tag in item["tags"][:4])
    note = (item.get("note") or "").strip()
    note_block = (
        f'<div class="note-line">{esc(note)}</div>'
        if ignored and note.startswith("自动忽略")
        else ""
    )
    title = esc(item["title"])
    verdict = esc(item["verdict"])
    verdict_badge = (
        '<span class="v warn">异常低价</span>'
        if item.get("suspicious")
        else f'<span class="v {item["verdict_class"]}">{verdict}</span>'
    )
    watch = esc(item.get("watch") or "")
    location = esc(item.get("location") or "未知")
    age = esc(item["age"])
    seller = f' · {esc(item["seller"])}' if item.get("seller") else ""
    new = ' <span class="new">NEW</span>' if item.get("is_new") else ""
    tags_block = f'<div class="tag-list">{tags}</div>' if tags else ""
    reasons_block = f'<div class="reasons">{reasons}</div>' if reasons else ""
    flags_block = f'<div class="flags">{flags}</div>' if flags else ""

    return f"""<article class="card vc-{item['verdict_class']}">
  {_thumb(item)}
  <div class="body">
    <div class="head">
      <a class="title" href="{url}" target="_blank" rel="noreferrer">{title}{new}</a>
      <div class="price">{money(item['price'])}<span class="ref">参考 {money(item['ref_price'])}</span></div>
    </div>
    <div class="badges">
      <span class="sb">{item['score']:.0f}<small>分</small></span>
      {verdict_badge}
      <span class="disc">低 {item['discount']:.0%}</span>
    </div>
    <div class="meta-line">{watch} · {location} · {age}{seller}</div>
    {tags_block}
    {reasons_block}
    {flags_block}
    {note_block}
    <div class="acts">{buttons}{link}</div>
  </div>
</article>"""


def _panel(name: str, cards: str, empty_text: str) -> str:
    if cards:
        body = f'<div class="grid">{cards}</div>'
    else:
        body = f'<div class="empty">{empty_text}</div>'
    return f'<section class="panel" data-panel="{name}">{body}</section>'


def _page(cfg: Config, db_path: Path) -> str:
    conn = connect(db_path)
    now = _now_ms()
    pending = [row_to_item(row, now) for row in rows_pending(conn, 400)]
    suspicious = [row_to_item(row, now) for row in rows_suspicious(conn, 100)]
    ignored = [row_to_item(row, now) for row in rows_ignored(conn, 200)]
    conn.close()
    items = pending + suspicious
    items.sort(key=lambda item: item["score"], reverse=True)
    ignored.sort(key=lambda item: item["score"], reverse=True)
    for item in items + ignored:
        item["is_new"] = item.get("first_seen") == item.get("last_seen")

    panels = _panel(
        "pending",
        "".join(_card(item) for item in items),
        "暂无待审核商品。先跑一次 scan。",
    ) + _panel(
        "ignored",
        "".join(_card(item, ignored=True) for item in ignored),
        "还没有忽略过商品。",
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>闲鱼选品 · 人工审核</title><style>{CSS}{REVIEW_CSS}</style></head>
<body>
<header class="top">
  <div class="top-row">
    <div>
      <h1>闲鱼选品 · 人工审核</h1>
      <div class="meta">数据保存在本机 data/xianyu.db · 只监听 127.0.0.1</div>
    </div>
    <div class="top-actions">
      <div class="stats">
        <span><b>{len(items)}</b>待审核</span>
        <span><b>{len(ignored)}</b>已忽略</span>
      </div>
      <button class="chip" id="refresh" type="button">刷新</button>
    </div>
  </div>
  <nav class="tabs">
    <button class="tab" type="button" data-tab="pending">待审核</button>
    <button class="tab" type="button" data-tab="ignored">已忽略</button>
  </nav>
</header>
<main>{panels}</main>
<div id="toast"></div>
<script>{REVIEW_JS}</script>
</body></html>"""


def make_handler(cfg: Config, db_path: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "XianyuReview/1.0"

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                body = _page(cfg, db_path).encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
            elif parsed.path == "/health":
                self._send(200, b'{"ok": true}', "application/json")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/api/status":
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
                item_id = str(data.get("item_id") or "")
                status = str(data.get("status") or "")
                if not item_id or status not in STATUS_LABELS:
                    raise ValueError("参数不完整")
                conn = connect(db_path)
                updated = set_status(conn, item_id, status)
                conn.close()
                payload = {
                    "ok": updated,
                    "message": f"已标记为「{STATUS_LABELS[status]}」" if updated else "没找到这条商品",
                }
            except Exception as exc:  # noqa: BLE001
                payload = {"ok": False, "message": f"保存失败：{exc}"}
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    return Handler


def serve(cfg: Config, port: int = 8765, open_browser: bool = True, db_path: Path | None = None) -> None:
    cfg.ensure_dirs()
    db_path = db_path or cfg.db_path
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg, db_path))
    url = f"http://127.0.0.1:{port}/"
    print(f"审核页已启动：{url}")
    print("按 Ctrl+C 停止。")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
