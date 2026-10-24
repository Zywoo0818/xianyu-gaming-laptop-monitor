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
from .parse import format_age, format_time
from .report import (
    CSS,
    _seller_url,
    ignored_row_to_reject_item,
    is_pro_seller,
    money,
    row_to_item,
    seller_signal_note,
)
from .store import (
    MANUAL_REJECT_REASON,
    RESCREEN_NOTE_PREFIX,
    STATUS_LABELS,
    connect,
    latest_scan,
    mark_manual_reject,
    restore_pending,
    rows_ignored,
    rows_pending,
    rows_rejects,
    rows_manual_rejects,
    set_status,
)

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
.badge{display:inline-block;border-radius:999px;padding:2px 8px;font-size:12px;font-weight:700}
.badge.reject{background:#fef2f2;color:#b91c1c;border:1px solid #fecaca}
.badge.manual{background:#fffbeb;color:#92400e;border:1px solid #fde68a}
.badge.pro{background:#fef2f2;color:#b91c1c;border:1px solid #fecaca}
.badge.identity{background:#eef2ff;color:#3730a3;border:1px solid #c7d2fe}
.why-line{margin-top:6px;font-size:12.5px;color:#b91c1c}
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
  var TABS=['pending','rejects','ignored'];

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


def _card(item: dict, *, ignored: bool = False, review_hint: int = 0) -> str:
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
    seller_link = (
        f'<a class="act link" href="{esc(item["seller_url"], quote=True)}" '
        'target="_blank" rel="noreferrer">卖家主页</a>'
        if item.get("seller_url")
        else ""
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
    badges = '<span class="v ok">待审核</span>'
    if is_pro_seller(item, review_hint):
        badges += (
            f'<span class="badge pro">疑似职业卖家：{int(item.get("seller_reviews") or 0)} 条评价</span>'
        )
    identity = str(item.get("seller_identity") or "").strip()
    if identity:
        badges += f'<span class="badge identity">{esc(identity)}</span>'
    signal = seller_signal_note(item)
    signal_block = f'<div class="meta-line">{esc(signal)}</div>' if signal else ""
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
      <div class="price">{money(item['price'])}</div>
    </div>
    <div class="badges">{badges}</div>
    <div class="meta-line">{watch} · {location} · {age}{seller}</div>
    {signal_block}
    {tags_block}
    {reasons_block}
    {flags_block}
    {note_block}
    <div class="acts">{buttons}{link}{seller_link}</div>
  </div>
</article>"""


def _reject_card(item: dict, now_ms: int) -> str:
    esc = html.escape
    reason_raw = str(item.get("reason") or "")
    manual = reason_raw == MANUAL_REJECT_REASON
    rescreened = reason_raw.startswith(RESCREEN_NOTE_PREFIX)
    item_id = esc(str(item["item_id"]), quote=True)
    url = esc(str(item["url"]), quote=True)
    seller_url = _seller_url(str(item.get("seller_id") or ""))
    seller_link = (
        f'<a class="act link" href="{esc(seller_url, quote=True)}" '
        'target="_blank" rel="noreferrer">卖家主页</a>'
        if seller_url
        else ""
    )
    restore = (
        f'<button class="act restore" data-id="{item_id}" '
        'data-status="pending">恢复待审核</button>'
        if (manual or rescreened)
        else ""
    )
    if manual:
        badge = '<span class="badge manual">人工审核</span>'
    elif rescreened:
        badge = '<span class="badge manual">积压重筛</span>'
    else:
        badge = '<span class="badge reject">粗筛排除</span>'
    meta = " · ".join(
        part
        for part in (
            esc(item.get("watch") or ""),
            esc(item.get("location") or "地区未知"),
            esc(format_age(int(item.get("publish_ts") or 0), now_ms)),
            esc(item.get("seller") or "卖家未知"),
        )
        if part
    )
    reason = esc(item.get("reason") or "未记录原因")
    time_text = esc(format_time(int(item.get("publish_ts") or 0)))
    return f"""<article class="card vc-skip">
  {_thumb(item)}
  <div class="body">
    <div class="head">
      <a class="title" href="{url}" target="_blank" rel="noreferrer">{esc(item['title'])}</a>
      <div class="price">{money(float(item.get('price') or 0))}</div>
    </div>
    <div class="badges">{badge}</div>
    <div class="meta-line">{meta}</div>
    <div class="why-line">不符合原因：{reason}</div>
    <div class="meta-line">{time_text}</div>
    <div class="acts">{restore}<a class="act link" href="{url}" target="_blank" rel="noreferrer">打开闲鱼</a>{seller_link}</div>
  </div>
</article>"""


def _panel(name: str, cards: str, empty_text: str) -> str:
    if cards:
        body = f'<div class="grid">{cards}</div>'
    else:
        body = f'<div class="empty">{empty_text}</div>'
    return f'<section class="panel" data-panel="{name}">{body}</section>'


def _pending_order(item: dict, now_ms: int, max_age_hours: float) -> tuple[int, int]:
    """D4 待审核排序键：时间窗内靠前（新→旧），过期沉底，发布时间未知排最后。

    只影响审核页展示顺序，不动数据库和静态报表。
    """
    ts = int(item.get("publish_ts") or 0)
    if ts <= 0:
        return (2, 0)
    if max_age_hours > 0 and (now_ms - ts) > max_age_hours * 3_600_000:
        return (1, -ts)
    return (0, -ts)


def _page(cfg: Config, db_path: Path) -> str:
    conn = connect(db_path)
    now = _now_ms()
    items = [row_to_item(row, now) for row in rows_pending(conn, 1000)]
    ignored = [row_to_item(row, now) for row in rows_ignored(conn, 200)]
    scan = latest_scan(conn)
    rejected = (
        [dict(row) for row in rows_rejects(conn, int(scan["id"]))] if scan else []
    )
    if scan:
        # 跨轮保留：人工忽略 / 积压重筛写在它们当时那一轮，新扫描后会被冲掉
        rejected += [
            ignored_row_to_reject_item(row, now)
            for row in rows_manual_rejects(conn, int(scan["id"]))
        ]
    conn.close()
    hint = cfg.scan.seller_review_hint
    for item in items + ignored:
        item["is_new"] = item.get("first_seen") == item.get("last_seen")
        item["pro_seller"] = is_pro_seller(item, hint)
    # 先按首次出现倒序，再按发布时间分组（稳定排序保持组内次序）：追新 + 过期沉底。
    items.sort(key=lambda item: str(item.get("first_seen") or ""), reverse=True)
    items.sort(key=lambda item: _pending_order(item, now, cfg.scan.max_age_hours))
    ignored.sort(key=lambda item: str(item.get("first_seen") or ""), reverse=True)
    rejected.sort(
        key=lambda item: (
            0 if item.get("reason") == MANUAL_REJECT_REASON else 1,
            float(item.get("price") or 0),
        )
    )

    panels = (
        _panel(
            "pending",
            "".join(_card(item, review_hint=hint) for item in items),
            "暂无待审核商品。先跑一次 scan。",
        )
        + _panel(
            "rejects",
            "".join(_reject_card(item, now) for item in rejected),
            "最新一轮没有不符合商品。",
        )
        + _panel(
            "ignored",
            "".join(_card(item, ignored=True, review_hint=hint) for item in ignored),
            "还没有自动忽略的商品。",
        )
    )
    scan_note = f" · 不符合来自扫描 #{scan['id']}" if scan else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>闲鱼选品 · 人工审核</title><style>{CSS}{REVIEW_CSS}</style></head>
<body>
<header class="top">
  <div class="top-row">
    <div>
      <h1>闲鱼选品 · 人工审核</h1>
      <div class="meta">数据保存在本机 data/xianyu.db · 只监听 127.0.0.1{scan_note}</div>
    </div>
    <div class="top-actions">
      <div class="stats">
        <span><b>{len(items)}</b>待审核</span>
        <span><b>{len(rejected)}</b>不符合</span>
        <span><b>{len(ignored)}</b>自动忽略</span>
      </div>
      <button class="chip" id="refresh" type="button">刷新</button>
    </div>
  </div>
  <nav class="tabs">
    <button class="tab" type="button" data-tab="pending">待审核</button>
    <button class="tab" type="button" data-tab="rejects">不符合</button>
    <button class="tab" type="button" data-tab="ignored">自动忽略</button>
  </nav>
</header>
<main>{panels}</main>
<div id="toast"></div>
<script>{REVIEW_JS}</script>
</body></html>"""


MAX_BODY_BYTES = 64 * 1024


def make_handler(cfg: Config, db_path: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "XianyuReview/1.0"

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            return

        def _port(self) -> int:
            return int(self.server.server_address[1])  # type: ignore[attr-defined]

        def _host_ok(self) -> bool:
            # 防 DNS rebinding：Host 必须是本机地址，而不是攻击者域名解析到 127.0.0.1
            host = str(self.headers.get("Host") or "").strip().lower()
            if not host:
                return False
            try:
                parsed = urlparse(f"//{host}")
                if (parsed.hostname or "") not in {"127.0.0.1", "localhost"}:
                    return False
                return parsed.port in (None, self._port())
            except ValueError:
                return False

        def _allowed_origin(self) -> str:
            """只放行 file:// 静态报表（null）和本审核页自身的源。"""
            origin = str(self.headers.get("Origin") or "").strip()
            if not origin:
                return ""
            if origin == "null":
                return "null"
            try:
                parsed = urlparse(origin)
                if parsed.scheme != "http":
                    return ""
                if (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost"}:
                    return ""
                if parsed.port != self._port():
                    return ""
            except ValueError:
                return ""
            return origin

        def _cross_site(self) -> bool:
            origin = self.headers.get("Origin")
            return bool(origin) and not self._allowed_origin()

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            origin = self._allowed_origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(body)

        def _forbidden(self) -> None:
            self._send(403, b"forbidden", "text/plain; charset=utf-8")

        def do_OPTIONS(self) -> None:  # noqa: N802
            if not self._host_ok() or not self._allowed_origin():
                self._forbidden()
                return
            self._send(204, b"", "text/plain; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok() or self._cross_site():
                self._forbidden()
                return
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                body = _page(cfg, db_path).encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
            elif parsed.path == "/health":
                self._send(200, b'{"ok": true}', "application/json")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok() or self._cross_site():
                self._forbidden()
                return
            if urlparse(self.path).path != "/api/status":
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            if not str(self.headers.get("Content-Type") or "").lower().startswith(
                "application/json"
            ):
                self._forbidden()
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0 or length > MAX_BODY_BYTES:
                    raise ValueError("请求体过大")
                data = json.loads(self.rfile.read(length) or b"{}")
                item_id = str(data.get("item_id") or "")
                status = str(data.get("status") or "")
                if not item_id or status not in STATUS_LABELS:
                    raise ValueError("参数不完整")
                conn = connect(db_path)
                if status == "ignored":
                    updated = mark_manual_reject(conn, item_id)
                    message = "已移入不符合（人工审核）"
                elif status == "pending":
                    updated = restore_pending(conn, item_id)
                    message = "已恢复待审核"
                else:
                    updated = set_status(conn, item_id, status)
                    message = f"已标记为「{STATUS_LABELS[status]}」"
                conn.close()
                payload = {
                    "ok": updated,
                    "message": message if updated else "没找到这条商品",
                }
            except Exception as exc:  # noqa: BLE001
                payload = {"ok": False, "message": f"保存失败：{exc}"}
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    return Handler


def serve(
    cfg: Config,
    port: int | None = None,
    open_browser: bool = True,
    db_path: Path | None = None,
) -> None:
    cfg.ensure_dirs()
    port = cfg.review.port if port is None else int(port)
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
