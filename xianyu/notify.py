"""可选推送：企业微信群机器人 / Bark。不配置就完全不发。

推送按“至少一次”处理：只有成功发出的条目才会记入 listings.notified_at，
发送失败的下次扫描还会在待推送队列里重试，不会重复轰炸已经发过的商品。
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Config
from .report import money

WECOM_LIMIT = 8
BARK_LIMIT = 5

Sender = Callable[[str, dict[str, Any]], tuple[bool, str]]


@dataclass
class NotifyResult:
    messages: list[str] = field(default_factory=list)
    sent_ids: set[str] = field(default_factory=set)


def _check_response(text: str) -> tuple[bool, str]:
    """企业微信和 Bark 失败时也可能返回 HTTP 200，必须看响应体里的状态码。"""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return False, f"响应不是合法 JSON：{text[:160]}"
    if not isinstance(data, dict):
        return False, f"响应格式异常：{text[:160]}"
    if "errcode" in data and data.get("errcode") not in (0, "0"):
        return False, f"errcode={data.get('errcode')} {data.get('errmsg', '')}".strip()
    if "code" in data and data.get("code") not in (200, "200"):
        return False, f"code={data.get('code')} {data.get('message', '')}".strip()
    if "errcode" not in data and "code" not in data:
        return False, f"响应缺少状态字段：{text[:160]}"
    return True, text[:200]


def _post_json(url: str, payload: dict[str, Any], timeout: int = 15) -> tuple[bool, str]:
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
        return _check_response(text)
    except Exception as exc:  # noqa: BLE001 - 推送失败只记录，不影响扫描
        return False, str(exc)[:200]


def _wecom_payload(items: list[dict], title: str) -> dict[str, Any]:
    lines = [f"### {title}"]
    for item in items[:WECOM_LIMIT]:
        extra = []
        if item.get("ref_price"):
            extra.append(f"参考 {money(item['ref_price'])}")
        if item.get("score"):
            extra.append(f"{item['score']:.0f} 分")
        tail = (" / " + " / ".join(extra)) if extra else ""
        lines.append(
            f"- [{item['title'][:38]}]({item['url']})\n"
            f"  <font color=\"info\">{money(item['price'])}</font>{tail}"
        )
    lines.append("\n> 详情见 reports/latest.html")
    content = "\n".join(lines)
    if len(content.encode("utf-8")) > 3900:
        content = content.encode("utf-8")[:3800].decode("utf-8", "ignore") + "…"
    return {"msgtype": "markdown", "markdown": {"content": content}}


def push(
    cfg: Config,
    items: list[dict],
    title: str = "闲鱼低价提醒",
    *,
    sender: Sender | None = None,
) -> NotifyResult:
    result = NotifyResult()
    if not cfg.notify.enabled:
        return result
    selected = [
        item
        for item in items
        if not item["suspicious"]
        and item["score"] >= cfg.notify.min_score
        and not item.get("notified_at")
    ]
    if not selected:
        result.messages.append("没有达到推送分数线的新商品，跳过推送")
        return result

    send = sender or _post_json
    if cfg.notify.wecom_webhook:
        batch = selected[:WECOM_LIMIT]
        ok, detail = send(cfg.notify.wecom_webhook, _wecom_payload(selected, title))
        result.messages.append("企业微信推送成功" if ok else f"企业微信推送失败：{detail}")
        if ok:
            result.sent_ids.update(str(item["item_id"]) for item in batch)
    if cfg.notify.bark_url:
        batch = selected[:BARK_LIMIT]
        text = "\n".join(
            f"{item['title'][:30]} {money(item['price'])}"
            + (f"（{item['score']:.0f}分）" if item.get("score") else "")
            + f"\n{item['url']}"
            for item in batch
        )
        ok, detail = send(
            cfg.notify.bark_url.rstrip("/"),
            {"title": f"{title} · 本次 {len(batch)} 条", "body": text, "group": "闲鱼"},
        )
        result.messages.append("Bark 推送成功" if ok else f"Bark 推送失败：{detail}")
        if ok:
            result.sent_ids.update(str(item["item_id"]) for item in batch)
    return result
