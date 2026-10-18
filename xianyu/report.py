"""生成给人看的报表：HTML（点链接直接跳闲鱼）、Markdown、CSV。"""
from __future__ import annotations

import csv
import html
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .parse import format_age, format_time
from .store import STATUS_LABELS

VERDICT_CLASS = {
    "强烈推荐": "hot",
    "推荐": "good",
    "可看": "ok",
    "观察": "meh",
    "异常": "warn",
    "跳过": "skip",
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


def _load_dict(value: Any) -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def row_to_item(row: sqlite3.Row | dict, now_ms: int) -> dict:
    data = dict(row)
    data["reasons"] = _loads(data.get("reasons"), [])
    data["flags"] = _loads(data.get("flags"), [])
    data["tags"] = _loads(data.get("tags"), [])
    data["audit"] = _load_dict(data.get("audit"))
    data["suspicious"] = bool(data.get("suspicious"))
    data["score"] = float(data.get("score") or 0)
    data["price"] = float(data.get("price") or 0)
    data["ref_price"] = float(data.get("ref_price") or 0)
    data["ratio"] = float(data.get("ratio") or 1)
    data["age"] = format_age(int(data.get("publish_ts") or 0), now_ms)
    data["time"] = format_time(int(data.get("publish_ts") or 0))
    data["status_label"] = STATUS_LABELS.get(data.get("status") or "pending", "待审核")
    data["verdict_class"] = VERDICT_CLASS.get(data.get("verdict") or "", "skip")
    data["status_class"] = STATUS_CLASS.get(data.get("status") or "pending", "pending")
    data["discount"] = 1 - data["ratio"] if data["ratio"] else 0.0
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
.title{font-weight:600;color:#111827;text-decoration:none;display:block;max-width:520px}
.title:hover{color:var(--blue);text-decoration:underline}
.sub{color:var(--muted);font-size:12px;margin-top:3px}
.price{font-size:17px;font-weight:700;white-space:nowrap}
.price small{font-weight:400;color:var(--muted);font-size:11.5px;display:block}
.off{color:var(--good);font-weight:600;font-size:12px}
.score{font-weight:700;font-size:16px}
.v{display:inline-block;font-size:11.5px;padding:1px 7px;border-radius:999px;margin-top:4px}
.v.hot{background:#fee2e2;color:#991b1b}
.v.good{background:#dcfce7;color:#166534}
.v.ok{background:#e0f2fe;color:#075985}
.v.meh{background:#f3f4f6;color:#4b5563}
.v.warn{background:#fef3c7;color:#92400e}
.v.skip{background:#f3f4f6;color:#6b7280}
.reason{color:#374151;font-size:12.5px}
.flag{color:var(--warn);font-size:12px;margin-top:3px}
.new{background:#dc2626;color:#fff;font-size:10px;padding:1px 5px;border-radius:4px;
margin-left:6px;vertical-align:2px}
.tag{display:inline-block;background:#eef2ff;color:#3730a3;font-size:11px;
padding:1px 6px;border-radius:4px;margin-right:4px}
.btn{display:inline-block;border:1px solid var(--line);border-radius:8px;padding:4px 10px;
font-size:12px;color:#374151;text-decoration:none;background:#fff;white-space:nowrap}
.btn:hover{border-color:#9ca3af}
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
  function apply(){
    var f=document.querySelector('[data-filter].on').dataset.filter;
    var term=(box&&box.value||'').trim().toLowerCase();
    document.querySelectorAll('tr[data-title]').forEach(function(tr){
      var okF = f==='all' || (f==='new'&&tr.dataset.new==='1') ||
                (f==='top'&&parseFloat(tr.dataset.score)>=75) ||
                (f==='susp'&&tr.dataset.susp==='1');
      var okT = !term || tr.dataset.title.indexOf(term)>=0;
      tr.style.display = (okF&&okT)?'':'none';
    });
  }
  chips.forEach(function(c){c.addEventListener('click',function(){
    chips.forEach(function(x){x.classList.remove('on')});
    c.classList.add('on'); apply();
  })});
  if(box) box.addEventListener('input',apply);
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
    flags = "；".join(html.escape(flag) for flag in item["flags"][:3])
    new_badge = '<span class="new">NEW</span>' if item.get("is_new") else ""
    discount = item["discount"]
    off = (
        f'<div class="off">低 {discount:.0%}</div>'
        if discount > 0.005
        else f'<div class="off" style="color:#6b7280">高 {-discount:.0%}</div>'
    )
    ref = money(item["ref_price"]) if item["ref_price"] else "—"
    title = html.escape(item["title"])
    gpu = html.escape(item.get("gpu") or "")
    brand = html.escape(item.get("brand") or "")
    table_name = html.escape(item.get("table_name") or "")
    audit = item.get("audit") or {}
    audit_badge = '<span class="tag" style="background:#fef3c7;color:#92400e">商家</span>' if audit.get("merchant") else ""
    return f"""
<tr data-title="{html.escape(item['title'].casefold(), quote=True)}" data-new="{1 if item.get('is_new') else 0}"
    data-score="{item['score']}" data-susp="{1 if item['suspicious'] else 0}">
  <td>{_thumb(item)}</td>
  <td>
    <a class="title" href="{html.escape(item['url'], quote=True)}" target="_blank" rel="noreferrer">{title}</a>
    <div class="sub">{f'<span class="tag">{gpu}</span>' if gpu else ''}{brand} · {tags}
      {html.escape(item.get('location') or '地区未知')} · {html.escape(item['age'])}
      · {html.escape(item.get('seller') or '卖家未知')}{audit_badge}</div>
    <div class="reason">{reasons}</div>
    {f'<div class="flag">{flags}</div>' if flags else ''}
  </td>
  <td class="price">{money(item['price'])}<small>{new_badge}</small></td>
  <td>{ref}<div class="sub">{table_name}</div></td>
  <td>{off}</td>
  <td><span class="score">{item['score']:.0f}</span>
      <div><span class="v {item['verdict_class']}">{html.escape(item['verdict'])}</span></div></td>
  <td><span class="st">{html.escape(item['status_label'])}</span><br>
      <a class="btn" href="{html.escape(item['url'], quote=True)}" target="_blank" rel="noreferrer">打开</a></td>
</tr>"""


def _table(items: Iterable[dict]) -> str:
    rows = "".join(_row(item) for item in items)
    return f"""<table>
<thead><tr><th></th><th>商品</th><th>价格</th><th>参考价</th><th>折价</th><th>评分</th><th>状态 / 操作</th></tr></thead>
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
) -> str:
    good = [item for item in items if not item["suspicious"]]
    suspicious = [item for item in items if item["suspicious"]]
    new_count = sum(1 for item in items if item.get("is_new"))

    sections = []
    if good:
        sections.append(
            f'<section><h2>🎯 值得看 <span class="n">{len(good)} 条</span></h2>{_table(good)}</section>'
        )
    else:
        sections.append(
            f'<section><h2>🎯 值得看</h2><div class="empty">{html.escape(empty_hint or "本轮没有符合条件的新商品。可以调低 scan.min_score，或放宽 market.target_factor。")}</div></section>'
        )
    if suspicious:
        sections.append(
            f'<section><h2>⚠️ 异常低价待核实 <span class="n">{len(suspicious)} 条 · 先看是不是定金/配件/引流</span></h2>{_table(suspicious)}</section>'
        )

    review = (
        f'<a class="btn" href="{html.escape(review_url, quote=True)}" target="_blank">打开审核页</a>'
        if review_url
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
  <div class="meta">抓取 <b>{stats.get('fetched', 0)}</b> 条 · 入选 <b>{stats.get('kept', 0)}</b> 条 ·
    本轮新增 <b>{new_count}</b> 条 · 关键词 <b>{stats.get('watches', 0)}</b> 个
    {f" · 耗时 {stats['elapsed']:.0f}s" if stats.get("elapsed") else ""}</div>
  <div class="bar">
    <button class="chip on" data-filter="all">全部</button>
    <button class="chip" data-filter="new">只看新增</button>
    <button class="chip" data-filter="top">评分 ≥ 75</button>
    <button class="chip" data-filter="susp">异常低价</button>
    <input id="q" type="search" placeholder="搜标题 / 配置…">
    {review}
  </div>
</header>
<main>
{''.join(sections)}
<div class="note">
  <b>怎么用：</b>点「打开」跳闲鱼核对实拍图和描述，先看卖家是否个人自用、有没有暗病/维修史，
  再聊价。评分只是把「价格 + 新鲜度 + 标题信号」量化的排序工具，最终以你的判断为准。
  低价异常区里往往是定金、配件、故障机，也可能真捡漏——务必先核实。
</div>
<footer>闲鱼笔记本监控 · 仅用于个人选品比价，请控制频率、遵守平台规则。</footer>
</main>
<script>{JS}</script>
</body></html>"""


def render_markdown(items: list[dict], *, title: str, generated_at: str, top_n: int = 20) -> str:
    lines = [f"# {title}", "", f"生成于 {generated_at}", ""]
    good = [item for item in items if not item["suspicious"]][:top_n]
    if not good:
        lines.append("本轮没有符合条件的新商品。")
    for index, item in enumerate(good, start=1):
        badge = " 🆕" if item.get("is_new") else ""
        lines.append(
            f"{index}. [{item['title']}]({item['url']}) — **{money(item['price'])}**"
            f"（参考 {money(item['ref_price'])}，低 {item['discount']:.0%}，{item['score']:.0f} 分）{badge}"
        )
        detail = f"   {item.get('gpu') or ''} · {item.get('brand') or ''} · {item.get('location') or '地区未知'} · {item['age']}"
        if item.get("seller"):
            detail += f" · {item['seller']}"
        lines.append(detail)
        if item["reasons"]:
            lines.append("   " + "；".join(item["reasons"][:3]))
    suspicious = [item for item in items if item["suspicious"]][:10]
    if suspicious:
        lines.extend(["", "## 异常低价（先核实是否定金/配件/引流）", ""])
        for item in suspicious:
            lines.append(
                f"- [{item['title']}]({item['url']}) — {money(item['price'])}"
                f"（参考 {money(item['ref_price'])}）"
            )
    return "\n".join(lines) + "\n"


CSV_FIELDS = [
    ("item_id", "商品ID"),
    ("title", "标题"),
    ("price", "价格"),
    ("ref_price", "行情表参考价"),
    ("table_name", "命中型号"),
    ("gpu", "显卡"),
    ("brand", "品牌"),
    ("discount", "折价比例"),
    ("score", "评分"),
    ("verdict", "结论"),
    ("suspicious", "异常"),
    ("watch", "监控项"),
    ("location", "地区"),
    ("seller", "卖家"),
    ("age", "发布时间"),
    ("status_label", "审核状态"),
    ("url", "链接"),
    ("reasons", "理由"),
    ("flags", "风险"),
]


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
                elif key == "discount":
                    value = f"{value:.1%}"
                elif key == "suspicious":
                    value = "是" if value else "否"
                row.append(value)
            writer.writerow(row)


def write_all(
    reports_dir: Path,
    items: list[dict],
    *,
    title: str,
    subtitle: str,
    stats: dict[str, Any],
    review_url: str = "",
) -> dict[str, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    html_text = render_html(
        items,
        title=title,
        subtitle=subtitle,
        stats=stats,
        generated_at=generated,
        review_url=review_url,
    )
    html_path = reports_dir / f"scan_{stamp}.html"
    html_path.write_text(html_text, encoding="utf-8")
    latest_html = reports_dir / "latest.html"
    latest_html.write_text(html_text, encoding="utf-8")

    md_path = reports_dir / "latest.md"
    md_path.write_text(
        render_markdown(items, title=title, generated_at=generated, top_n=int(stats.get("top_n", 20))),
        encoding="utf-8",
    )
    csv_path = reports_dir / f"scan_{stamp}.csv"
    write_csv(csv_path, items)
    shutil.copyfile(csv_path, reports_dir / "latest.csv")
    return {"html": html_path, "latest_html": latest_html, "md": md_path, "csv": csv_path}
