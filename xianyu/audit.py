"""卖家审核：看卖家主页在卖什么，识别骗子和商家。

规则（来自你的要求）：
- 主页出现大疆 / DJI / 无人机 → 直接排除（百分百骗子）
- 主页大量苹果设备 → 减分
- 主页是回收/实体店/批量 → 商家，重减分

做法：只审“已经通过品类和价差、分数靠前”的少量商品，避免把每次扫描拖成半小时。
每个卖家审一次，结果缓存 7 天。
"""
from __future__ import annotations

import os
import random
import re
import time
from pathlib import Path
from typing import Callable

from playwright.sync_api import Page, sync_playwright

from .config import Config
from .crawler import default_user_agent, prepare_browser_temp
from .models import Listing
from .pricetable import load_table, match_reference
from .pricing import gate, laptop_like
from .store import connect, get_seller_audit, save_seller_audit

DJI_WORDS = ("大疆", "dji", "无人机", "osmo", "御3", "mini3", "air3", "穿越机")
APPLE_WORDS = ("iphone", "ipad", "macbook", "airpods", "apple watch", "苹果")
MERCHANT_WORDS = ("高价收", "回收", "实体店", "门头", "质保", "批发", "店铺", "大量到货", "长期收")

ITEM_URL = "https://www.goofish.com/item?id={item_id}"


def pick_candidates(cfg: Config, listings: list[Listing], limit: int | None = None) -> list[Listing]:
    """挑出值得花时间审卖家的商品：过闸门、够便宜、按折扣排序。"""
    if not cfg.audit.enabled:
        return []
    rows = load_table(cfg.price_table.path) if cfg.price_table else []
    scored: list[tuple[float, Listing]] = []
    for listing in listings:
        result = gate(listing, cfg)
        if not result.ok or result.gpu is None:
            continue
        ref, _, source = match_reference(listing.title, result.gpu.model, rows)
        if not ref:
            continue
        if not laptop_like(listing.title, source):
            continue
        gap = ref - listing.price
        if gap < cfg.scan.min_gap:
            continue
        scored.append((gap / ref, listing))
    scored.sort(key=lambda pair: -pair[0])

    picked: list[Listing] = []
    seen: set[str] = set()
    for _, listing in scored:
        key = listing.seller or listing.item_id
        if key in seen:
            continue
        seen.add(key)
        picked.append(listing)
        if len(picked) >= (limit or cfg.audit.max_items):
            break
    return picked


def _seller_link(page: Page) -> str:
    links = page.eval_on_selector_all("a", "els => els.map(e => e.href)")
    for link in links:
        if "/personal?userId=" in link:
            return link
    match = re.search(r"userId=(\d+)", page.content())
    if match:
        return f"https://www.goofish.com/personal?userId={match.group(1)}"
    return ""


def audit_one(page: Page, listing: Listing, scroll_times: int = 3) -> dict:
    data: dict = {
        "user_id": "",
        "has_dji": False,
        "apple_hits": 0,
        "merchant": False,
        "in_sale": 0,
        "sold": 0,
        "bio": "",
        "titles": [],
    }
    page.goto(ITEM_URL.format(item_id=listing.item_id), wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(random.randint(1500, 2600))
    link = _seller_link(page)
    if not link:
        data["error"] = "找不到卖家主页链接"
        return data
    data["user_id"] = link.split("userId=")[-1].split("&")[0]

    page.goto(link, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(random.randint(1800, 2800))
    for _ in range(max(1, scroll_times)):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1200)

    text = page.inner_text("body")
    lowered = text.casefold()
    data["has_dji"] = any(word in lowered for word in DJI_WORDS)
    data["apple_hits"] = sum(1 for word in APPLE_WORDS if word in lowered)
    data["merchant"] = any(word in text for word in MERCHANT_WORDS)

    match = re.search(r"在售\s*(\d+)", text)
    if match:
        data["in_sale"] = int(match.group(1))
    match = re.search(r"已售出\s*(\d+)", text)
    if match:
        data["sold"] = int(match.group(1))
    if data["in_sale"] > 40 or data["sold"] > 500:
        data["merchant"] = True

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    data["titles"] = [line for line in lines if 8 <= len(line) <= 60][:30]
    data["bio"] = " / ".join(data["titles"][:2])
    return data


def audit_sellers(
    cfg: Config,
    listings: list[Listing],
    logger: Callable[[str], None] = print,
) -> dict[str, dict]:
    candidates = pick_candidates(cfg, listings)
    if not candidates:
        return {}
    logger(f"  卖家审核：挑出 {len(candidates)} 个候选（每个卖家看一次主页）")

    conn = connect(cfg.db_path)
    results: dict[str, dict] = {}
    todo: list[Listing] = []
    for listing in candidates:
        cached = get_seller_audit(conn, listing.seller) if listing.seller else None
        if cached:
            results[listing.item_id] = cached
        elif listing.seller:
            todo.append(listing)

    if todo:
        prepare_browser_temp(cfg)
        with sync_playwright() as pw:
            options = {
                "user_data_dir": str(cfg.browser.profile_dir),
                "headless": cfg.browser.headless,
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
                "viewport": {"width": 1360, "height": 900},
                "args": ["--disable-blink-features=AutomationControlled", "--no-first-run"],
                "ignore_default_args": ["--enable-automation"],
            }
            if cfg.browser.channel:
                options["channel"] = cfg.browser.channel
            if cfg.browser.headless:
                options["user_agent"] = cfg.browser.user_agent or default_user_agent()
            ctx = pw.chromium.launch_persistent_context(**options)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            for index, listing in enumerate(todo, start=1):
                logger(f"    [{index}/{len(todo)}] 查卖家：{listing.seller or '未知'}（{listing.title[:20]}…）")
                try:
                    data = audit_one(page, listing, cfg.audit.scroll_times)
                except Exception as exc:  # noqa: BLE001
                    logger(f"      ✗ 审核失败：{type(exc).__name__}")
                    continue
                if data.get("has_dji"):
                    logger("      ⚠ 主页有大疆，按骗子排除")
                elif data.get("merchant"):
                    logger("      · 判定为商家")
                results[listing.item_id] = data
                if listing.seller:
                    save_seller_audit(conn, listing.seller, data)
                time.sleep(random.uniform(2.0, 4.0))
            ctx.close()
    conn.close()
    return results
