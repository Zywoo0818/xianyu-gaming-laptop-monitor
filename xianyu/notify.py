"""可选推送：企业微信群机器人 / Bark。不配置就完全不发。"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from .config import Config
from .report import money


def _post_json(url: str, payload: dict[str, Any], timeout: int = 15) -> tuple[bool, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
        return True, text[:200]
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


def _wecom_payload(items: list[dict], title: str) -> dict[str, Any]:
    lines = [f"### {title}"]
    for item in items[:8]:
        lines.append(
            f"- [{item['title'][:38]}]({item['url']})\n"
            f"  <font color=\"info\">{money(item['price'])}</font>"
            f" / 参考 {money(item['ref_price'])} / {item['score']:.0f} 分"
        )
    lines.append("\n> 详情见 reports/latest.html")
    content = "\n".join(lines)
    if len(content.encode("utf-8")) > 3900:
        content = content.encode("utf-8")[:3800].decode("utf-8", "ignore") + "…"
    return {"msgtype": "markdown", "markdown": {"content": content}}


def push(cfg: Config, items: list[dict], title: str = "闲鱼低价提醒") -> list[str]:
    if not cfg.notify.enabled:
        return []
    selected = [
        item
        for item in items
        if not item["suspicious"] and item["score"] >= cfg.notify.min_score
    ]
    if not selected:
        return ["没有达到推送分数线的商品，跳过推送"]
    messages: list[str] = []
    if cfg.notify.wecom_webhook:
        ok, detail = _post_json(cfg.notify.wecom_webhook, _wecom_payload(selected, title))
        messages.append(("企业微信推送成功" if ok else f"企业微信推送失败：{detail}"))
    if cfg.notify.bark_url:
        text = "\n".join(
            f"{item['title'][:30]} {money(item['price'])}（{item['score']:.0f}分）\n{item['url']}"
            for item in selected[:5]
        )
        ok, detail = _post_json(
            cfg.notify.bark_url.rstrip("/"),
            {"title": f"{title} · {len(selected)} 条", "body": text, "group": "闲鱼"},
        )
        messages.append("Bark 推送成功" if ok else f"Bark 推送失败：{detail}")
    return messages
