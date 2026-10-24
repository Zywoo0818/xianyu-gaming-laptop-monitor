"""v3 细筛：只看卖家主页首屏，不再回头判断搜索页内容。

规则：
- 卖家其他商品标题出现「大疆」→ 直接排除
- 卖家主页首屏里笔记本/电脑配件 ≥5 条 → 按商家排除
- 其他情况通过；主页商品数量不足或加载失败由调用方标记为未完成
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config
from .models import SellerItem, SellerPage
from .pricing import GAMING_SERIES, LAPTOP_WORDS

LAPTOP_BRANDS = (
    "机械革命", "机械师", "雷神", "神舟", "七彩虹", "未来人类", "火影",
)
_SCREEN_SIZE = re.compile(r"\d{1,2}(?:\.\d)?\s*寸")


@dataclass
class FineResult:
    verdict: str  # pass / dji / merchant
    reason: str
    item_count: int = 0
    laptop_count: int = 0
    part_count: int = 0
    in_sale: int = 0
    matched_item: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict == "pass"


def classify_item(title: str, cfg: Config) -> str:
    """把卖家主页的一条商品分成 laptop / part / other。"""
    lowered = title.casefold()
    if any(word.casefold() in lowered for word in LAPTOP_WORDS):
        return "laptop"
    if any(series.casefold() in lowered for series in GAMING_SERIES):
        return "laptop"
    if any(brand.casefold() in lowered for brand in LAPTOP_BRANDS):
        return "laptop"
    if _SCREEN_SIZE.search(title):
        return "laptop"
    if any(word.casefold() in lowered for word in cfg.fine.part_words):
        return "part"
    return "other"


def _hit_dji(items: list[SellerItem], cfg: Config) -> tuple[SellerItem, str] | None:
    patterns = [re.compile(pattern, re.IGNORECASE) for pattern in cfg.fine.dji_patterns]
    for item in items:
        for pattern in patterns:
            match = pattern.search(item.title)
            if match:
                label = "大疆" if "大疆" in match.group(0) else "DJI"
                return item, label
    return None


def evaluate_page(page: SellerPage, cfg: Config) -> FineResult:
    items = page.items[: cfg.fine.item_limit]
    laptop_count = 0
    part_count = 0
    for item in items:
        kind = classify_item(item.title, cfg)
        if kind == "laptop":
            laptop_count += 1
        elif kind == "part":
            part_count += 1

    base = dict(
        item_count=len(items),
        laptop_count=laptop_count,
        part_count=part_count,
        in_sale=page.in_sale,
    )

    dji = _hit_dji(items, cfg)
    if dji is not None:
        item, label = dji
        return FineResult(
            verdict="dji",
            reason=f"卖家其他商品命中「{label}」：{item.title[:40]}",
            matched_item=item.item_id,
            **base,
        )

    computer_items = laptop_count + part_count
    if computer_items >= cfg.fine.merchant_min_items:
        return FineResult(
            verdict="merchant",
            reason=(
                f"商家模块2：卖家主页 {computer_items} 条笔记本/电脑配件"
                f"（≥{cfg.fine.merchant_min_items}）"
            ),
            **base,
        )

    if not items:
        reason = "细筛通过：卖家主页明确显示没有在售商品"
    else:
        reason = (
            f"细筛通过：主页 {len(items)} 条，笔记本/配件 {computer_items} 条"
        )
        if page.in_sale > 0:
            reason += f"，在售 {page.in_sale} 条"
    return FineResult(verdict="pass", reason=reason, **base)


def cache_fresh(checked_ts: int, hours: float, now_ts: int) -> bool:
    if hours <= 0 or checked_ts <= 0:
        return False
    return now_ts - checked_ts <= hours * 3600
