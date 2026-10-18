"""筛选与打分：只做“游戏本 + N 卡 + 行情表比价”。

流程：
1. 品类闸门：苹果/华为排除；工作站、轻薄本、台式机排除；必须识别到英伟达显卡。
2. 参考价：先用你的行情表按“型号 + 显卡”匹配，匹配不到用同显卡中位数。
3. 价差：必须比参考价低至少 min_gap（默认 100），否则直接不看。
4. 打分：价差深度 + 显卡档位 + 品牌权重 + 新鲜度 + 卖家质量 - 想要人数 - 商家/骗子风险。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from . import brands, gpus
from .config import Config, Watch
from .models import Analysis, Listing
from .pricetable import PriceRow, match_reference
from .textmatch import hit_words

VERDICTS = ("强烈推荐", "推荐", "可看", "观察", "跳过", "异常", "排除")

LAPTOP_WORDS = ("笔记本", "游戏本", "本本", "laptop", "notebook", "手提")
DESKTOP_HINTS = ("8pin", "6pin", "三风扇", "单卡", "矿卡", "显卡坞", "机箱", "水冷", "台式")

# 常见游戏本系列：标题里出现这些，即使没写“笔记本”也认
GAMING_SERIES = (
    "拯救者", "r7000", "r9000", "y7000", "y9000", "legion", "geekpro",
    "天选", "飞行堡垒", "rog", "魔霸", "枪神", "幻14", "幻16",
    "暗影精灵", "光影精灵", "omen", "游匣", "g15", "g16", "外星人", "alienware",
    "暗影骑士", "掠夺者", "nitro", "predator",
    "蛟龙", "旷世", "极光", "钛钽", "将星", "隐星", "战神", "炫龙", "雷神", "机械师",
    "未来人类", "火影", "ge62", "ge75", "gp75", "gl75", "ge66", "gs66", "msi",
)


def _spec_adjust(title: str, ref: float) -> tuple[float, list[str]]:
    """按标题里的内存/硬盘容量对行情表价格做加减（只在有明确上下文时生效）。"""
    import re

    lowered = title.casefold()
    delta = 0.0
    notes: list[str] = []

    ram_match = re.search(r"(?:内存|ram|ddr\d?)\s*[:：]?\s*(\d{1,2})\s*g", lowered)
    if not ram_match:
        ram_match = re.search(r"(?<!\d)(\d{1,2})\s*g\s*(?:内存|ram|ddr\d?)", lowered)
    if ram_match:
        ram = int(ram_match.group(1))
        value = {4: -300, 8: -150, 16: 0, 32: 200, 64: 400}.get(ram, 0)
        if value:
            delta += value
            notes.append(f"{ram}G 内存 {value:+.0f}")

    disk_match = re.search(r"(?:硬盘|固态|ssd|nvme)\s*[:：]?\s*(\d{2,4})\s*g", lowered)
    if not disk_match:
        disk_match = re.search(r"(?<!\d)(\d{2,4})\s*g\s*(?:硬盘|固态|ssd|nvme)", lowered)
    if disk_match:
        disk = int(disk_match.group(1))
        value = {64: -250, 128: -200, 256: -100, 512: 0, 1024: 150}.get(disk, 0)
        if value:
            delta += value
            notes.append(f"{disk}G 硬盘 {value:+.0f}")
    else:
        disk_t = re.search(r"(?:硬盘|固态|ssd|nvme)\s*[:：]?\s*([12])\s*t", lowered)
        if disk_t:
            value = 150 if disk_t.group(1) == "1" else 300
            delta += value
            notes.append(f"{disk_t.group(1)}T 硬盘 {value:+.0f}")

    cap = ref * 0.25
    return max(-cap, min(cap, delta)), notes


@dataclass
class Gate:
    ok: bool
    reason: str
    gpu: gpus.GpuInfo | None
    brand: brands.BrandInfo


def gate(listing: Listing, cfg: Config) -> Gate:
    title = listing.title
    lowered = title.casefold()
    brand = brands.detect(title, listing.seller)
    if brand.excluded:
        return Gate(False, f"{brand.name}品牌，按要求排除", None, brand)

    gpu = gpus.detect(title)
    if gpu is not None and gpu.is_workstation:
        return Gate(False, "工作站/专业卡，不是游戏本", gpu, brand)
    if gpu is not None and not gpu.is_nvidia:
        return Gate(False, "非英伟达显卡，排除", gpu, brand)
    if gpu is None:
        return Gate(False, "识别不到英伟达显卡，排除", None, brand)

    if any(word.casefold() in lowered for word in cfg.category.exclude):
        if "游戏本" not in lowered:
            return Gate(False, "标题显示是轻薄本/商务本/台式机", gpu, brand)
    if any(word.casefold() in lowered for word in DESKTOP_HINTS):
        return Gate(False, "台式机显卡/配件，不是整机", gpu, brand)
    # 台式显卡的描述特征：显卡 + 风扇/接口/供电/这张
    if "显卡" in lowered and any(
        word in lowered for word in ("风扇", "接口", "这张", "插上", "供电", "散热", "挡板")
    ):
        return Gate(False, "台式显卡，不是笔记本", gpu, brand)
    return Gate(True, "", gpu, brand)


def _condition(title: str, cfg: Config) -> tuple[float, list[str], list[str]]:
    """成色加减分；返回 (分数, 理由, 风险标签)。"""
    score = 0.0
    reasons: list[str] = []
    flags: list[str] = []

    good = hit_words(title, cfg.signals.good)
    if good:
        score += min(4.0, 1.5 * len(good))
        reasons.append("正面：" + "、".join(good[:3]))

    bad = hit_words(title, cfg.signals.bad)
    if bad:
        score -= min(12.0, 4.0 * len(bad))
        flags.append("瑕疵：" + "、".join(bad[:3]))

    merchant = hit_words(title, cfg.signals.merchant)
    if merchant:
        score += cfg.scoring.merchant_penalty
        flags.append("疑似商家：" + "、".join(merchant[:2]))

    bait = hit_words(title, cfg.signals.bait)
    if bait:
        flags.append("引流话术：" + "、".join(bait[:3]))

    if hit_words(title, cfg.signals.broken):
        flags.append("故障/配件描述")
        score -= 15

    return score, reasons, flags


def _freshness(listing: Listing, now_ms: int) -> tuple[float, str]:
    if listing.publish_ts <= 0:
        return 0.0, ""
    hours = max(0.0, (now_ms - listing.publish_ts) / 3_600_000)
    if hours <= 2:
        return 10.0, "2 小时内新上架"
    if hours <= 6:
        return 7.0, f"{hours:.0f} 小时内上架"
    if hours <= 24:
        return 4.0, "今天上架"
    if hours <= 72:
        return 1.0, "3 天内上架"
    return 0.0, ""


def _wants(listing: Listing, cfg: Config) -> tuple[float, str]:
    if listing.wants >= 100:
        return cfg.scoring.wants_penalty_high, f"{listing.wants} 人想要，抢的人太多"
    if listing.wants >= 50:
        return cfg.scoring.wants_penalty_low, f"{listing.wants} 人想要"
    return 0.0, ""


def laptop_like(title: str, ref_source: str = "") -> bool:
    """判断标题像不像一台整机笔记本（用来挡掉台式显卡/配件）。"""
    lowered = title.casefold()
    has_spec = bool(
        re.search(r"(i[3579][- ]?\d{3,5}|r[3579][- ]?\d{3,4}|\d{3,4}h\b|ultra\s?\d|n\d{3,4})", lowered)
        or re.search(r"(内存|硬盘|固态|ddr)", lowered)
        or ("屏幕" in lowered and "寸" in lowered)
    )
    return bool(
        (any(word.casefold() in lowered for word in LAPTOP_WORDS) and has_spec)
        or any(series in lowered for series in GAMING_SERIES)
        or ref_source == "table/model"
    )


def analyze(
    listing: Listing,
    watch: Watch,
    table_rows: list[PriceRow],
    cfg: Config,
    now_ms: int | None = None,
    audit: dict | None = None,
) -> Analysis:
    now_ms = now_ms or int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    result = gate(listing, cfg)
    if not result.ok:
        return Analysis(
            score=0.0,
            verdict="排除",
            ref_price=0.0,
            ratio=1.0,
            flags=[result.reason],
            excluded=True,
        )
    assert result.gpu is not None

    ref, table_name, ref_source = match_reference(listing.title, result.gpu.model, table_rows)
    if ref is None and watch.reference > 0:
        ref, table_name, ref_source = watch.reference, "配置里的 reference", "watch"
    if not ref:
        return Analysis(score=0.0, verdict="排除", ref_price=0.0, ratio=1.0, flags=["没有参考价"], excluded=True)

    lowered = listing.title.casefold()
    is_laptop = laptop_like(listing.title, ref_source)
    if not is_laptop:
        return Analysis(
            score=0.0,
            verdict="排除",
            ref_price=ref,
            ratio=round(listing.price / ref, 4),
            flags=["看不出是笔记本整机（可能是台式显卡/配件）"],
            excluded=True,
            gpu=result.gpu.model,
            brand=result.brand.name,
            table_name=table_name,
            ref_source=ref_source,
        )

    spec_delta, spec_notes = _spec_adjust(listing.title, ref)
    ref = ref + spec_delta

    gap = ref - listing.price
    if gap < cfg.scan.min_gap:
        message = (
            f"比行情表还高 ¥{-gap:,.0f}，不看"
            if gap < 0
            else f"只低 ¥{gap:,.0f}，未到 ¥{cfg.scan.min_gap:,.0f} 底线"
        )
        return Analysis(
            score=0.0,
            verdict="价差不足",
            ref_price=ref,
            ratio=round(listing.price / ref, 4),
            flags=[message],
            excluded=True,
            market_median=ref,
            gpu=result.gpu.model,
            brand=result.brand.name,
            table_name=table_name,
            ref_source=ref_source,
        )

    score = cfg.scoring.base
    reasons: list[str] = []
    flags: list[str] = []

    price_score = min(cfg.scoring.price_max, max(0.0, (gap - cfg.scan.min_gap) / ref * 260))
    score += price_score
    if spec_delta:
        reasons.append(
            f"行情表 ¥{ref - spec_delta:,.0f}（{table_name}），配置折算 ¥{ref:,.0f}，低 ¥{gap:,.0f}"
        )
    else:
        reasons.append(f"行情表 ¥{ref:,.0f}（{table_name}），低 ¥{gap:,.0f}")
    reasons.extend(spec_notes)

    score += result.gpu.score
    if result.gpu.score:
        reasons.append(f"{result.gpu.model} 显卡 {'加分' if result.gpu.score > 0 else '减分'} {result.gpu.score:+.0f}")

    score += result.brand.weight
    if result.brand.weight:
        reasons.append(f"{result.brand.name} 优先品牌 {result.brand.weight:+.0f}")

    fresh_score, fresh_note = _freshness(listing, now_ms)
    score += fresh_score
    if fresh_note:
        reasons.append(fresh_note)

    wants_score, wants_note = _wants(listing, cfg)
    score += wants_score
    if wants_note:
        flags.append(wants_note)

    condition_score, condition_reasons, condition_flags = _condition(listing.title, cfg)
    score += condition_score
    reasons.extend(condition_reasons)
    flags.extend(condition_flags)

    if "验货宝" in listing.tags:
        score += 2
        reasons.append("验货宝")

    suspicious = False
    bait = hit_words(listing.title, cfg.signals.bait)
    broken = hit_words(listing.title, cfg.signals.broken)
    if bait or broken:
        suspicious = True
        score = min(score, 35.0)

    if audit:
        if audit.get("has_dji"):
            return Analysis(
                score=0.0,
                verdict="排除",
                ref_price=ref,
                ratio=round(listing.price / ref, 4),
                flags=["卖家在卖大疆/无人机，按要求判定为骗子"],
                excluded=True,
                gpu=result.gpu.model,
                brand=result.brand.name,
                table_name=table_name,
                ref_source=ref_source,
            )
        if audit.get("apple_hits", 0) >= 3:
            score -= 8
            flags.append(f"卖家还在卖 {audit['apple_hits']} 件苹果设备")
        if audit.get("merchant"):
            score += cfg.scoring.audited_merchant_penalty
            flags.append("卖家是商家/回收号（主页确认）")
        if audit.get("in_sale", 0) > 40:
            score -= 4
            flags.append(f"在售 {audit['in_sale']} 件，明显是商家")

    score = round(max(0.0, min(100.0, score)), 1)

    if suspicious:
        verdict = "异常"
    elif score >= 80:
        verdict = "强烈推荐"
    elif score >= 68:
        verdict = "推荐"
    elif score >= cfg.scan.min_score:
        verdict = "可看"
    else:
        verdict = "跳过"

    return Analysis(
        score=score,
        verdict=verdict,
        ref_price=round(ref, 2),
        ratio=round(listing.price / ref, 4),
        reasons=reasons,
        flags=flags,
        suspicious=suspicious,
        market_median=round(ref, 2),
        gpu=result.gpu.model,
        gpu_score=result.gpu.score,
        brand=result.brand.name,
        brand_score=result.brand.weight,
        table_name=table_name,
        ref_source=ref_source,
    )


def passes_filters(listing: Listing, watch: Watch, cfg: Config) -> bool:
    if listing.price < watch.min_price or listing.price > watch.max_price:
        return False
    if listing.price < cfg.filters.min_price or listing.price > cfg.filters.max_price:
        return False
    if cfg.filters.location_allow and not any(
        word in listing.location for word in cfg.filters.location_allow if word
    ):
        return False
    if any(word.casefold() in listing.title.casefold() for word in cfg.filters.exclude if word):
        return False
    return True


def is_fresh(listing: Listing, max_age_hours: float, now_ms: int) -> bool:
    if max_age_hours <= 0 or listing.publish_ts <= 0:
        return True
    return (now_ms - listing.publish_ts) / 3_600_000 <= max_age_hours
