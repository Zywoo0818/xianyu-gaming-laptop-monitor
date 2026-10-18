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
from .store import STATUS_LABELS, connect, rows_pending, rows_suspicious, set_status

ACTIONS = [
    ("adopted", "采纳", "#15803d"),
    ("contacted", "已联系", "#1d4ed8"),
    ("done", "已成交", "#6b7280"),
    ("ignored", "忽略", "#b91c1c"),
]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _card(item: dict) -> str:
    reasons = "；".join(html.escape(reason) for reason in item["reasons"][:3])
    flags = "；".join(html.escape(flag) for flag in item["flags"][:3])
    buttons = "".join(
        f'<button class="act" data-id="{html.escape(item["item_id"], quote=True)}"'
        f' data-status="{status}" style="color:{color};border-color:{color}33">{label}</button>'
        for status, label, color in ACTIONS
    )
    return f"""
<div class="card" data-score="{item['score']}">
  <div class="row1">
    <a class="title" href="{html.escape(item['url'], quote=True)}" target="_blank" rel="noreferrer">{html.escape(item['title'])}</a>
    <div class="p">{money(item['price'])}<span class="ref">参考 {money(item['ref_price'])}</span></div>
  </div>
  <div class="meta2">
    <span class="s">{item['score']:.0f} 分</span>
    <span class="v {item['verdict_class']}">{html.escape(item['verdict'])}</span>
    {html.escape(item['watch'])} · {html.escape(item.get('location') or '未知')} · {html.escape(item['age'])}
    {(' · ' + html.escape(item['seller'])) if item.get('seller') else ''}
     <span class="off">低 {item['discount']:.0%}</span>
  </div>
  <div class="rz">{reasons}</div>
  {f'<div class="fl">{flags}</div>' if flags else ''}
  <div class="acts">{buttons}
    <a class="act link" href="{html.escape(item['url'], quote=True)}" target="_blank" rel="noreferrer">打开闲鱼</a>
  </div>
</div>"""


def _page(cfg: Config, db_path: Path) -> str:
    conn = connect(db_path)
    now = _now_ms()
    pending = [row_to_item(row, now) for row in rows_pending(conn, 400)]
    suspicious = [row_to_item(row, now) for row in rows_suspicious(conn, 100)]
    conn.close()
    cards = "".join(_card(item) for item in pending) or '<div class="empty">暂无待审核商品。先跑一次 scan。</div>'
    susp = "".join(_card(item) for item in suspicious)
    susp_block = (
        f'<section><h2>⚠️ 异常低价待核实 <span class="n">{len(suspicious)}</span></h2>{susp}</section>'
        if susp else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>闲鱼选品 · 人工审核</title><style>{CSS}
.card{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px}}
.row1{{display:flex;gap:14px;justify-content:space-between;align-items:flex-start}}
.row1 .title{{max-width:none}}
.p{{font-size:18px;font-weight:700;white-space:nowrap}}
.p .ref{{display:block;font-size:11.5px;color:var(--muted);font-weight:400;text-align:right}}
.meta2{{color:var(--muted);font-size:12px;margin-top:4px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
.meta2 .s{{font-weight:700;color:#111827;font-size:14px}}
.off{{color:var(--good);font-weight:600}}
.rz{{font-size:12.5px;margin-top:6px;color:#374151}}
.fl{{font-size:12px;color:var(--warn);margin-top:2px}}
.acts{{margin-top:9px;display:flex;gap:6px;flex-wrap:wrap}}
.act{{border:1px solid #d1d5db;background:#fff;border-radius:7px;padding:4px 12px;font-size:12px;
cursor:pointer;color:#374151;text-decoration:none}}
.act:hover{{background:#f9fafb}}
.act.link{{color:#1d4ed8}}
.done{{opacity:.45;pointer-events:none}}
#toast{{position:fixed;right:18px;bottom:18px;background:#111827;color:#fff;padding:8px 14px;
border-radius:8px;font-size:12.5px;opacity:0;transition:.2s}}
#toast.on{{opacity:1}}
</style></head>
<body>
<header>
  <h1>闲鱼选品 · 人工审核</h1>
  <div class="meta">待审核 <b>{len(pending)}</b> 条 · 异常低价 <b>{len(suspicious)}</b> 条 ·
    点按钮记录结果，数据保存在本机 data/xianyu.db</div>
  <div class="bar">
    <button class="chip on" data-f="all">全部</button>
    <button class="chip" data-f="top">评分 ≥ 75</button>
    <input id="q" type="search" placeholder="搜标题…">
  </div>
</header>
<main>
<section><h2>🎯 待审核 <span class="n">{len(pending)}</span></h2>{cards}</section>
{susp_block}
</main>
<div id="toast"></div>
<script>
(function(){{
  function toast(msg){{var t=document.getElementById('toast');t.textContent=msg;t.classList.add('on');
    setTimeout(function(){{t.classList.remove('on')}},1600)}}
  document.addEventListener('click',function(e){{
    var b=e.target.closest('.act[data-status]'); if(!b) return;
    var card=b.closest('.card');
    b.disabled=true;
    fetch('/api/status',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{item_id:b.dataset.id,status:b.dataset.status}})}})
      .then(function(r){{return r.json()}})
      .then(function(d){{ if(d.ok){{card.classList.add('done');toast(d.message||'已记录')}} else {{toast(d.message||'失败');b.disabled=false}} }})
      .catch(function(){{toast('保存失败，检查终端是否还在运行');b.disabled=false}});
  }});
  document.querySelectorAll('.chip[data-f]').forEach(function(c){{
    c.addEventListener('click',function(){{
      document.querySelectorAll('.chip[data-f]').forEach(function(x){{x.classList.remove('on')}});
      c.classList.add('on');
      var f=c.dataset.f;
      document.querySelectorAll('.card').forEach(function(card){{
        card.style.display=(f==='all'||parseFloat(card.dataset.score)>=75)?'':'none';
      }});
    }});
  }});
  var box=document.getElementById('q');
  box&&box.addEventListener('input',function(){{
    var t=box.value.trim().toLowerCase();
    document.querySelectorAll('.card').forEach(function(card){{
      card.style.display=(!t||card.querySelector('.title').textContent.toLowerCase().indexOf(t)>=0)?'':'none';
    }});
  }});
}})();
</script></body></html>"""


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
