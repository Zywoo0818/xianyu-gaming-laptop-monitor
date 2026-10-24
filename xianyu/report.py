"""生成给人看的报表：HTML（点链接直接跳闲鱼）、Markdown、CSV。

v3.6 报表分两块：待审核（粗筛通过）+ 不符合（被粗筛/价格规则排除，带原因）。
价格筛选/评分关闭时不出现参考价、折价、分数列。
"""
from __future__ import annotations

import csv
import html
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from . import selectors as sel
from .config import DEFAULT_REVIEW_PORT
from .models import Listing
from .parse import format_age, format_time
from .store import STATUS_LABELS
from . import __version__ as _VERSION

VERDICT_CLASS = {
    "强烈推荐": "hot",
    "推荐": "good",
    "可看": "ok",
    "观察": "meh",
    "异常": "warn",
    "跳过": "skip",
    "待审核": "ok",
}

STATUS_CLASS = {
    "pending": "pending",
    "adopted": "adopted",
    "contacted": "contacted",
    "done": "done",
    "ignored": "ignored",
}


def _loads(value: Any, fallback: list) -> list:
    if not value:
        return fallback
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, list) else fallback


def _seller_url(seller_id: str) -> str:
    if not seller_id:
        return ""
    return sel.SELLER_URL.format(seller_id=quote(str(seller_id), safe=""))


def row_to_item(row: sqlite3.Row | dict, now_ms: int) -> dict:
    data = dict(row)
    data["reasons"] = _loads(data.get("reasons"), [])
    data["flags"] = _loads(data.get("flags"), [])
    data["tags"] = _loads(data.get("tags"), [])
    data["suspicious"] = bool(data.get("suspicious"))
    data["score"] = float(data.get("score") or 0)
    data["price"] = float(data.get("price") or 0)
    data["ref_price"] = float(data.get("ref_price") or 0)
    data["ratio"] = float(data.get("ratio") or 1)
    data["seller_reviews"] = int(data.get("seller_reviews") or 0)
    data["seller_rating"] = int(data.get("seller_rating") or 0)
    data["seller_identity"] = str(data.get("seller_identity") or "").strip()
    data["age"] = format_age(int(data.get("publish_ts") or 0), now_ms)
    data["time"] = format_time(int(data.get("publish_ts") or 0))
    data["status_label"] = STATUS_LABELS.get(data.get("status") or "pending", "待审核")
    data["verdict_class"] = VERDICT_CLASS.get(data.get("verdict") or "", "ok")
    data["status_class"] = STATUS_CLASS.get(data.get("status") or "pending", "pending")
    data["discount"] = 1 - data["ratio"] if data["ratio"] else 0.0
    data["seller_url"] = _seller_url(str(data.get("seller_id") or ""))
    return data


def seller_signal_note(item: dict) -> str:
    """卖家画像标注（D2，只标注不排除）：评价数 / 好评率 / 平台身份。无信号返回空串。"""
    reviews = int(item.get("seller_reviews") or 0)
    rating = int(item.get("seller_rating") or 0)
    identity = str(item.get("seller_identity") or "").strip()
    if not reviews and not rating and not identity:
        return ""
    parts = []
    if reviews:
        parts.append(f"评价 {reviews}")
    if rating:
        parts.append(f"好评率 {rating}%")
    if identity:
        parts.append(identity)
    return " · ".join(parts)


def is_pro_seller(item: dict, hint: int) -> bool:
    """评价数达到标注阈值（scan.seller_review_hint）→ 疑似职业卖家。

    只用于展示提醒，不参与任何排除逻辑；阈值可配，0 = 关闭。
    """
    return hint > 0 and int(item.get("seller_reviews") or 0) >= hint


def rejected_item(listing: Listing, reason: str, now_ms: int) -> dict:
    return {
        "item_id": listing.item_id,
        "title": listing.title,
        "price": float(listing.price or 0),
        "url": listing.url,
        "image": listing.image,
        "location": listing.location,
        "seller": listing.seller,
        "seller_id": listing.seller_id,
        "seller_url": _seller_url(listing.seller_id),
        "publish_ts": listing.publish_ts,
        "watch": listing.watch,
        "keyword": listing.keyword,
        "age": format_age(listing.publish_ts, now_ms),
        "time": format_time(listing.publish_ts),
        "reason": reason,
    }


def reject_row_to_item(row: sqlite3.Row | dict, now_ms: int) -> dict:
    data = dict(row)
    data["price"] = float(data.get("price") or 0)
    data["seller_url"] = _seller_url(str(data.get("seller_id") or ""))
    data["age"] = format_age(int(data.get("publish_ts") or 0), now_ms)
    data["time"] = format_time(int(data.get("publish_ts") or 0))
    return data


def ignored_row_to_reject_item(row: sqlite3.Row | dict, now_ms: int) -> dict:
    """人工忽略 / 重筛移出的商品 → 按「不符合」卡片渲染。

    `listings.note` 里存的是完整原因（`人工审核` 或 `重筛排除：<原因>`），
    这里直接拿来当不符合原因展示，跨轮也能保留。
    """
    data = reject_row_to_item(row, now_ms)
    note = str(data.get("note") or "").strip()
    data["reason"] = note or "未记录原因"
    return data


def money(value: float) -> str:
    return f"¥{value:,.0f}"


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--muted:#6b7280;--line:#e5e7eb;
--good:#15803d;--hot:#b91c1c;--warn:#b45309;--blue:#1d4ed8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{position:sticky;top:0;z-index:9;background:rgba(255,255,255,.94);
backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:14px 20px}
h1{margin:0 0 6px;font-size:19px;letter-spacing:.2px}
.meta{color:var(--muted);font-size:12.5px}
.meta b{color:var(--ink)}
main{padding:16px 20px 60px;max-width:1280px;margin:0 auto}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 6px;align-items:center}
.chip{border:1px solid var(--line);background:#fff;border-radius:999px;padding:5px 12px;
cursor:pointer;font-size:13px;color:#374151}
.chip.on{background:#111827;color:#fff;border-color:#111827}
input[type=search]{border:1px solid var(--line);border-radius:999px;padding:6px 14px;
min-width:200px;font-size:13px;outline:none}
.prange{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;color:var(--muted)}
.prange label{white-space:nowrap}
.prange input[type=number]{border:1px solid var(--line);border-radius:999px;
padding:6px 10px;width:82px;font-size:13px;outline:none;background:#fff;color:#374151}
.prange input[type=number]:focus{border-color:#9ca3af}
.prange .dash{color:var(--muted)}
#filter-state{font-size:12px;color:var(--muted)}
section{margin-top:22px}
h2{font-size:15px;margin:0 0 10px;display:flex;align-items:center;gap:8px}
h2 .n{font-weight:400;color:var(--muted);font-size:12.5px}
table{width:100%;border-collapse:separate;border-spacing:0;background:var(--card);
border:1px solid var(--line);border-radius:12px;overflow:hidden}
th{text-align:left;font-size:12px;color:var(--muted);font-weight:600;
padding:10px 12px;background:#fafafa;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:11px 12px;border-bottom:1px solid #f1f2f4;vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fcfcfd}
.thumb{width:58px;height:58px;border-radius:8px;object-fit:cover;background:#eef0f3;
display:block}
.thumb.ph{display:flex;align-items:center;justify-content:center;color:#9ca3af;font-size:11px}
.title{font-weight:600;color:#111827;text-decoration:none;display:block;max-width:620px}
.title:hover{color:var(--blue);text-decoration:underline}
.sub{color:var(--muted);font-size:12px;margin-top:3px}
.price{font-size:17px;font-weight:700;white-space:nowrap}
.price small{font-weight:400;color:var(--muted);font-size:11.5px;display:block}
.reason{color:#374151;font-size:12.5px;margin-top:4px}
.flag{color:var(--warn);font-size:12px;margin-top:3px}
.new{background:#dc2626;color:#fff;font-size:10px;padding:1px 5px;border-radius:4px;
margin-left:6px;vertical-align:2px}
.why{color:#b91c1c;font-size:12.5px;margin-top:2px}
.tag{display:inline-block;background:#eef2ff;color:#3730a3;font-size:11px;
padding:1px 6px;border-radius:4px;margin-right:4px}
.btn{display:inline-block;border:1px solid var(--line);border-radius:8px;padding:4px 10px;
font-size:12px;color:#374151;text-decoration:none;background:#fff;white-space:nowrap}
.btn:hover{border-color:#9ca3af}
.btn.ignore{border-color:#fecaca;color:#b91c1c;background:#fef2f2;cursor:pointer;
font-family:inherit;margin-right:4px}
.btn.ignore:hover{border-color:#f87171}
.btn.ignore:disabled{opacity:.5;cursor:default}
tr.done td{opacity:.45}
tr.done .btn{pointer-events:none}
tr.done .btn.ignore{background:#f3f4f6;border-color:var(--line);color:#6b7280}
.st{font-size:11.5px;color:var(--muted)}
.empty{color:var(--muted);padding:16px;background:#fff;border:1px dashed var(--line);
border-radius:12px}
footer{color:#9ca3af;font-size:12px;margin-top:28px}
.note{background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px 14px;
color:#4b5563;font-size:12.5px;margin-top:18px}
"""

JS = """
(function(){
  var chips=document.querySelectorAll('[data-filter]');
  var box=document.getElementById('q');
  var pminEl=document.getElementById('pmin');
  var pmaxEl=document.getElementById('pmax');
  var shownEl=document.getElementById('shown');
  var totalEl=document.getElementById('total');
  var rejShownEl=document.getElementById('rej-shown');
  var rejTotalEl=document.getElementById('rej-total');
  var rangeEl=document.getElementById('range-text');
  var stateEl=document.getElementById('filter-state');
  var clearBtn=document.getElementById('pclear');
  var KEY='xianyu_report_price_range';

  function num(value){
    if(value===null||value===undefined)return null;
    var text=String(value).replace(/[^0-9.]/g,'');
    if(!text)return null;
    var parsed=parseFloat(text);
    return isNaN(parsed)?null:parsed;
  }
  function save(){
    try{
      localStorage.setItem(KEY,JSON.stringify({
        min:pminEl?pminEl.value:'',
        max:pmaxEl?pmaxEl.value:''
      }));
    }catch(_){}
  }
  function apply(){
    var f=document.querySelector('[data-filter].on').dataset.filter;
    var term=(box&&box.value||'').trim().toLowerCase();
    var low=num(pminEl&&pminEl.value);
    var high=num(pmaxEl&&pmaxEl.value);
    if(low!==null&&high!==null&&low>high){var swap=low;low=high;high=swap;}
    var tally={pending:{shown:0,total:0},rejected:{shown:0,total:0}};
    document.querySelectorAll('tr[data-title]').forEach(function(tr){
      var kind = tr.dataset.kind==='reject' ? 'rejected' : 'pending';
      tally[kind].total++;
      var isReject = tr.dataset.kind==='reject';
      var okF = isReject || f==='all' || (f==='new'&&tr.dataset.new==='1');
      var okT = !term || tr.dataset.title.indexOf(term)>=0;
      var price=num(tr.dataset.price);
      var okP = price===null ||
                ((low===null||price>=low)&&(high===null||price<=high));
      var ok = okF&&okT&&okP;
      if(ok)tally[kind].shown++;
      tr.style.display = ok?'':'none';
    });
    var filtering=(low!==null||high!==null);
    if(shownEl) shownEl.textContent=String(tally.pending.shown);
    if(totalEl) totalEl.textContent=String(tally.pending.total);
    if(rejShownEl) rejShownEl.textContent=String(tally.rejected.shown);
    if(rejTotalEl) rejTotalEl.textContent=String(tally.rejected.total);
    if(rangeEl){
      var parts=[];
      if(low!==null)parts.push('\u2265 \u00a5'+low);
      if(high!==null)parts.push('\u2264 \u00a5'+high);
      rangeEl.textContent=parts.join(' \u4e14 ');
    }
    if(stateEl) stateEl.hidden=!filtering;
  }

  function clearPrice(){
    if(pminEl) pminEl.value='';
    if(pmaxEl) pmaxEl.value='';
    save();
    apply();
  }

  chips.forEach(function(c){c.addEventListener('click',function(){
    chips.forEach(function(x){x.classList.remove('on')});
    c.classList.add('on'); apply();
  })});
  if(box) box.addEventListener('input',apply);
  if(pminEl) pminEl.addEventListener('input',function(){save();apply();});
  if(pmaxEl) pmaxEl.addEventListener('input',function(){save();apply();});
  if(clearBtn) clearBtn.addEventListener('click',clearPrice);

  // 恢复上次设置（本机 localStorage；file:// 下可能不可用，失败就算了）
  try{
    var saved=JSON.parse(localStorage.getItem(KEY)||'{}');
    if(pminEl&&saved.min)pminEl.value=saved.min;
    if(pmaxEl&&saved.max)pmaxEl.value=saved.max;
  }catch(_){}
  apply();

  // v3.8.4 报表「忽略」键：POST 到本地审核页服务（review.bat，端口取 config.review.port）。
  // 静态文件本身不写库；忽略成功后行置灰、计数即时更新，重新生成报表后正式进不符合板块。
  var API='http://127.0.0.1:__REVIEW_PORT__/api/status';
  function toast(msg){
    var t=document.getElementById('toast');
    if(!t){t=document.createElement('div');t.id='toast';
      t.style.cssText='position:fixed;right:18px;bottom:18px;background:#111827;color:#fff;'+
        'padding:9px 15px;border-radius:9px;font-size:12.5px;z-index:20';
      document.body.appendChild(t);}
    t.textContent=msg;t.style.opacity='1';
    clearTimeout(t._timer);
    t._timer=setTimeout(function(){t.style.opacity='0'},2200);
  }
  function bump(sel,delta){
    var el=document.querySelector(sel);
    if(!el)return;
    var n=parseInt(el.textContent,10);
    if(!isNaN(n)&&n+delta>=0)el.textContent=String(n+delta);
  }
  document.addEventListener('click',function(e){
    var button=e.target.closest('button.ignore[data-status="ignored"]');
    if(!button||button.disabled)return;
    var tr=button.closest('tr');
    button.disabled=true;
    fetch(API,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({item_id:button.dataset.id,status:'ignored'})})
      .then(function(response){return response.json()})
      .then(function(data){
        if(data.ok){
          if(tr)tr.classList.add('done');
          button.textContent='已忽略';
          toast('已移入不符合（重新生成报表后正式生效）');
          bump('#cnt-pending',-1);
          bump('#cnt-rejected',1);
          var sec=document.getElementById('sec-pending');
          if(sec){
            var n=parseInt(sec.textContent,10);
            if(!isNaN(n)&&n-1>=0)sec.textContent=sec.textContent.replace(String(n),String(n-1));
          }
        }else{
          toast(data.message||'失败');
          button.disabled=false;
        }
      })
      .catch(function(){
        toast('审核页服务未启动：请先运行 review.bat');
        button.disabled=false;
      });
  });
})();
"""


def _thumb(item: dict) -> str:
    image = (item.get("image") or "").strip()
    if not image:
        return '<div class="thumb ph">无图</div>'
    return f'<img class="thumb" loading="lazy" referrerpolicy="no-referrer" src="{html.escape(image, quote=True)}" alt="">'


def _row(item: dict) -> str:
    tags = "".join(f'<span class="tag">{html.escape(tag)}</span>' for tag in item["tags"][:3])
    reasons = "；".join(html.escape(reason) for reason in item["reasons"][:3])
    flags = "；".join(html.escape(flag) for flag in item["flags"][:3] if flag != "未细筛")
    badges = ""
    if item.get("is_new"):
        badges += '<span class="new">NEW</span>'
    title = html.escape(item["title"])
    url = html.escape(item["url"], quote=True)
    seller = html.escape(item.get("seller") or "卖家未知")
    seller_link = (
        f' · <a class="st" href="{html.escape(item["seller_url"], quote=True)}" '
        f'target="_blank" rel="noreferrer">卖家主页</a>'
        if item.get("seller_url")
        else ""
    )
    signal = seller_signal_note(item)
    signal_text = f' · <span class="st">{html.escape(signal)}</span>' if signal else ""
    pro_line = (
        f'<div class="flag">疑似职业卖家：{int(item.get("seller_reviews") or 0)} 条评价</div>'
        if item.get("pro_seller")
        else ""
    )
    return f"""
<tr data-title="{html.escape(item['title'].casefold(), quote=True)}" data-new="{1 if item.get('is_new') else 0}" data-price="{float(item['price'] or 0)}">
  <td>{_thumb(item)}</td>
  <td>
    <a class="title" href="{url}" target="_blank" rel="noreferrer">{title}{badges}</a>
    <div class="sub">{tags}{html.escape(item.get('watch') or '')} ·
      {html.escape(item.get('location') or '地区未知')} · {html.escape(item['age'])} · {seller}{seller_link}{signal_text}</div>
    <div class="reason">{reasons}</div>
    {f'<div class="flag">{flags}</div>' if flags else ''}
    {pro_line}
  </td>
  <td class="price">{money(item['price'])}</td>
  <td><span class="st">{html.escape(str(item.get('first_seen') or ''))}</span></td>
  <td><span class="st">{html.escape(item['status_label'])}</span><br>
      <button class="btn ignore" type="button" data-id="{html.escape(str(item['item_id']), quote=True)}" data-status="ignored">忽略</button>
      <a class="btn" href="{url}" target="_blank" rel="noreferrer">打开</a></td>
</tr>"""


def _table(items: Iterable[dict]) -> str:
    rows = "".join(_row(item) for item in items)
    return f"""<table>
<thead><tr><th></th><th>商品</th><th>价格</th><th>首次出现</th><th>状态 / 操作</th></tr></thead>
<tbody>{rows}</tbody></table>"""


def _reject_row(item: dict) -> str:
    title = html.escape(item["title"])
    url = html.escape(item["url"], quote=True)
    seller = html.escape(item.get("seller") or "卖家未知")
    seller_link = (
        f' · <a class="st" href="{html.escape(item["seller_url"], quote=True)}" '
        f'target="_blank" rel="noreferrer">卖家主页</a>'
        if item.get("seller_url")
        else ""
    )
    sub = " · ".join(
        part
        for part in (
            html.escape(item.get("watch") or ""),
            html.escape(item.get("keyword") or ""),
            html.escape(item.get("location") or ""),
            html.escape(item.get("age") or ""),
        )
        if part
    )
    return f"""
<tr data-kind="reject" data-title="{html.escape(item['title'].casefold(), quote=True)}" data-price="{float(item['price'] or 0)}">
  <td>{_thumb(item)}</td>
  <td>
    <a class="title" href="{url}" target="_blank" rel="noreferrer">{title}</a>
    <div class="sub">{sub}</div>
  </td>
  <td class="price">{money(item['price'])}</td>
  <td><span class="st">{seller}{seller_link}</span></td>
  <td><span class="why">{html.escape(item.get('reason') or '未记录原因')}</span></td>
  <td><span class="st">{html.escape(item.get('time') or '')}</span><br>
      <a class="btn" href="{url}" target="_blank" rel="noreferrer">打开</a></td>
</tr>"""


def _reject_table(items: Iterable[dict]) -> str:
    rows = "".join(_reject_row(item) for item in items)
    return f"""<table>
<thead><tr><th></th><th>商品</th><th>价格</th><th>卖家</th><th>不符合原因</th><th>发布时间 / 操作</th></tr></thead>
<tbody>{rows}</tbody></table>"""


def render_html(
    items: list[dict],
    *,
    title: str,
    subtitle: str,
    stats: dict[str, Any],
    generated_at: str,
    review_url: str = "",
    empty_hint: str = "",
    rejected: list[dict] | None = None,
    review_port: int = DEFAULT_REVIEW_PORT,
) -> str:
    rejected = rejected or []
    new_count = sum(1 for item in items if item.get("is_new"))
    review = (
        f'<a class="btn" href="{html.escape(review_url, quote=True)}" target="_blank">打开审核页</a>'
        if review_url
        else ""
    )
    sort_text = str(stats.get("sort_status") or "")
    sort_bad = any(
        token in sort_text
        for token in ("默认", "失败", "未记录", "未确认", "离开", "停止")
    )
    sort_line = (
        f'<div class="meta">排序：{"⚠ " if sort_bad else ""}<b>{html.escape(sort_text)}</b></div>'
        if sort_text
        else ""
    )
    coarse_text = (
        f"粗筛通过 <b>{stats['coarse']}</b> 条 · "
        if isinstance(stats.get("coarse"), int)
        else ""
    )
    section = (
        f'<section><h2>待审核 <span class="n" id="sec-pending" data-new="{new_count}">{len(items)} 条 · 新增 {new_count}</span></h2>{_table(items)}</section>'
        if items
        else f'<section><h2>待审核</h2><div class="empty">{html.escape(empty_hint or "当前没有待审核商品。下一轮扫描会自动补上。")}</div></section>'
    )
    reject_section = (
        f'<section><h2>不符合 <span class="n" id="sec-rejected" data-n="{len(rejected)}">{len(rejected)} 条 · 被粗筛/价格规则排除，点标题可核对</span></h2>'
        f"{_reject_table(rejected)}</section>"
        if rejected
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body>
<header>
  <h1>{html.escape(title)}</h1>
  <div class="meta">{html.escape(subtitle)} · 生成于 {html.escape(generated_at)}</div>
  <div class="meta">搜索 <b>{stats.get('fetched', 0)}</b> 条 · {coarse_text}不符合 <b id="cnt-rejected">{stats.get('rejected', len(rejected))}</b> 条 ·
    待审核 <b id="cnt-pending">{stats.get('kept', len(items))}</b> 条
    {f" · 耗时 {stats['elapsed']:.0f}s" if stats.get("elapsed") else ""}</div>
  {sort_line}
  <div class="bar">
    <button class="chip on" data-filter="all">全部</button>
    <button class="chip" data-filter="new">只看新增</button>
    <span class="prange">
      <label for="pmin">价格区间</label>
      <input id="pmin" type="number" min="0" step="100" placeholder="最低">
      <span class="dash">–</span>
      <input id="pmax" type="number" min="0" step="100" placeholder="最高">
      <button class="chip" id="pclear" type="button">清除</button>
    </span>
    <input id="q" type="search" placeholder="搜标题 / 配置…">
    {review}
  </div>
  <div class="meta" id="filter-state" hidden>价格区间 <b id="range-text"></b>：待审核显示 <b id="shown">0</b> / <span id="total">0</span> 条 ·
    不符合显示 <b id="rej-shown">0</b> / <span id="rej-total">0</span> 条</div>
</header>
<main>
{section}
{reject_section}
<div class="note">
  <b>流程：</b>只抓搜索页，进页面点「新发布 → 最新」，再按标题/介绍/卖家昵称粗筛
  （商家词、开机故障、问题机、华为/苹果、缺内存硬盘、低配等），发布时间超过 scan.max_age_hours 的商品也会进「不符合」。
  被筛掉的商品不会丢，全部列在「不符合」里并附原因；
  价格筛选和评分默认关闭。点「打开」核对实拍图和描述后再聊。
  <b>忽略键：</b>点待审核行的「忽略」会把商品移入不符合板块（需要审核页服务在运行——先双击 review.bat；
  静态文件本身不写库，重新生成报表后正式生效）。
</div>
<footer>闲鱼游戏本监控 v{_VERSION} · 登录态低频访问 · 仅用于个人选品比价，请控制频率、遵守平台规则。</footer>
</main>
<div id="toast" style="position:fixed;right:18px;bottom:18px;background:#111827;color:#fff;padding:9px 15px;border-radius:9px;font-size:12.5px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:20"></div>
<script>{JS.replace("__REVIEW_PORT__", str(int(review_port)))}</script>
</body></html>"""


def render_markdown(
    items: list[dict],
    *,
    title: str,
    generated_at: str,
    top_n: int = 50,
    sort_status: str = "",
    rejected: list[dict] | None = None,
) -> str:
    rejected = rejected or []
    lines = [f"# {title}", "", f"生成于 {generated_at}"]
    if sort_status:
        lines.append(f"排序：{sort_status}")
    lines.append("")
    if not items:
        lines.append("当前没有待审核商品。")
    else:
        lines.append(f"## 待审核（{len(items)} 条）")
        lines.append("")
        for index, item in enumerate(items[:top_n], start=1):
            badges = ["🆕"] if item.get("is_new") else []
            suffix = (" " + " ".join(badges)) if badges else ""
            lines.append(
                f"{index}. [{item['title']}]({item['url']}) — **{money(item['price'])}**{suffix}"
            )
            detail = (
                f"   {item.get('watch') or ''} · {item.get('location') or '地区未知'}"
                f" · {item['age']} · {item.get('seller') or '卖家未知'}"
            )
            lines.append(detail)
            if item["reasons"]:
                lines.append("   " + "；".join(item["reasons"][:3]))
            flags = [flag for flag in item["flags"] if flag != "未细筛"]
            if flags:
                lines.append("   ⚠ " + "；".join(flags[:3]))
        if len(items) > top_n:
            lines.append(f"\n……另有 {len(items) - top_n} 条，见 HTML/CSV。")
    if rejected:
        lines.extend(["", f"## 不符合（{len(rejected)} 条）", ""])
        for index, item in enumerate(rejected[:top_n], start=1):
            lines.append(
                f"{index}. [{item['title']}]({item['url']}) — **{money(item['price'])}**"
                f" · {item.get('seller') or '卖家未知'}"
            )
            lines.append(f"   不符合原因：{item.get('reason') or '未记录原因'}")
        if len(rejected) > top_n:
            lines.append(f"\n……另有 {len(rejected) - top_n} 条，见 HTML。")
    return "\n".join(lines) + "\n"


CSV_FIELDS = [
    ("item_id", "商品ID"),
    ("title", "标题"),
    ("price", "价格"),
    ("watch", "监控项"),
    ("location", "地区"),
    ("seller", "卖家"),
    ("seller_id", "卖家ID"),
    ("seller_reviews", "评价数"),
    ("seller_rating", "好评率"),
    ("seller_identity", "卖家身份"),
    ("age", "发布时间"),
    ("status_label", "审核状态"),
    ("first_seen", "首次出现"),
    ("last_seen", "最近出现"),
    ("is_new", "本轮新增"),
    ("url", "链接"),
    ("seller_url", "卖家主页"),
    ("reasons", "粗筛理由"),
    ("flags", "提示"),
]

REJECT_CSV_FIELDS = [
    ("item_id", "商品ID"),
    ("title", "标题"),
    ("price", "价格"),
    ("watch", "监控项"),
    ("keyword", "关键词"),
    ("location", "地区"),
    ("seller", "卖家"),
    ("seller_id", "卖家ID"),
    ("publish_ts", "发布时间戳"),
    ("reason", "不符合原因"),
    ("url", "链接"),
    ("seller_url", "卖家主页"),
]


CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """外部文本进 CSV 前防公式注入：Excel 会把 = + - @ 开头当公式执行。"""
    if isinstance(value, str) and value.startswith(CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def write_csv(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow([label for _, label in CSV_FIELDS])
        for item in items:
            row = []
            for key, _ in CSV_FIELDS:
                value = item.get(key, "")
                if key in {"reasons", "flags"}:
                    value = "；".join(value)
                elif key == "is_new":
                    value = "是" if value else "否"
                row.append(csv_safe(value))
            writer.writerow(row)


def write_reject_csv(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow([label for _, label in REJECT_CSV_FIELDS])
        for item in items:
            writer.writerow([csv_safe(item.get(key, "")) for key, _ in REJECT_CSV_FIELDS])


def write_all(
    reports_dir: Path,
    items: list[dict],
    *,
    title: str,
    subtitle: str,
    stats: dict[str, Any],
    review_url: str = "",
    rejected: list[dict] | None = None,
    review_port: int = DEFAULT_REVIEW_PORT,
) -> dict[str, Path]:
    rejected = rejected or []
    reports_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    generated = now.strftime("%Y-%m-%d %H:%M:%S")
    html_text = render_html(
        items,
        title=title,
        subtitle=subtitle,
        stats=stats,
        generated_at=generated,
        review_url=review_url,
        rejected=rejected,
        review_port=review_port,
    )
    html_path = reports_dir / f"scan_{stamp}.html"
    html_path.write_text(html_text, encoding="utf-8")
    latest_html = reports_dir / "latest.html"
    latest_html.write_text(html_text, encoding="utf-8")

    md_text = render_markdown(
        items,
        title=title,
        generated_at=generated,
        top_n=int(stats.get("top_n", 50)),
        sort_status=str(stats.get("sort_status") or ""),
        rejected=rejected,
    )
    md_path = reports_dir / f"scan_{stamp}.md"
    md_path.write_text(md_text, encoding="utf-8")
    latest_md = reports_dir / "latest.md"
    latest_md.write_text(md_text, encoding="utf-8")

    csv_path = reports_dir / f"scan_{stamp}.csv"
    write_csv(csv_path, items)
    shutil.copyfile(csv_path, reports_dir / "latest.csv")
    reject_csv = reports_dir / f"scan_{stamp}_rejects.csv"
    write_reject_csv(reject_csv, rejected)
    shutil.copyfile(reject_csv, reports_dir / "latest_rejects.csv")
    return {
        "html": html_path,
        "latest_html": latest_html,
        "md": md_path,
        "latest_md": latest_md,
        "csv": csv_path,
        "latest_csv": reports_dir / "latest.csv",
        "rejects_csv": reject_csv,
        "latest_rejects_csv": reports_dir / "latest_rejects.csv",
    }
