"""筛选与打分：只做“游戏本 + N 卡 + 行情表比价”。

流程：
1. 品类闸门：苹果/华为排除；工作站、轻薄本、台式机排除；必须识别到英伟达显卡。
2. 参考价：先用你的行情表按“型号 + 显卡”匹配，匹配不到用同显卡中位数。
3. 价差：必须比参考价低至少 min_gap（默认 100），否则直接不看。
4. 打分：价差深度 + 显卡档位 + 品牌权重 + 新鲜度 + 成色 - 想要人数 - 商家风险。
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
DESKTOP_HINTS = ("8pin", "6pin", "单卡", "矿卡", "显卡坞", "机箱")

FATAL_RAM_GB = 8
FATAL_DISK_GB = 120
# 「无内存插槽」「无硬盘位」是描述扩展能力，不代表缺件。
REJECT_BLOCKED_TAILS = ("位", "槽", "插", "卡", "接", "盖", "盒", "架", "托", "口")
_CAPABILITY_WORDS = ("最大", "最高", "支持", "可扩", "可加", "可升级", "可达", "扩展至", "加装")
_UPGRADE_PREFIXES = ("已升级", "已经升级", "已加装", "已经加装")
_RAM_CONTEXT = r"(?:运行内存|内存|ram|ddr[45]?)"
_DISK_CONTEXT = r"(?:固态硬盘|硬盘|固态|ssd|nvme|存储)"
_UPGRADE_VALUE = re.compile(
    r"(?:已升级到|已经升级到|已升级|已经升级|已加装|已经加装)\s*[:：]?\s*(\d{1,4})\s*([gt])"
)
_DISK_NOUNS = ("硬盘", "固态", "ssd", "nvme", "存储")

# 硬排除：即使标题写了“游戏本”，命中这些也直接丢（工作站/台式/求购等不是整机游戏本）。
# 否定句（无拆机/不是工作站）和对比句（比台式机轻）会被 hit_words / CONTRAST_PREFIXES 放行。
HARD_EXCLUDE = (
    "移动工作站", "工作站", "台式机", "台式", "迷你主机", "一体机",
    "求购", "收一台", "租赁", "出租", "拆机", "模型", "代拍",
)

# 常见游戏本系列：标题里出现这些，即使没写“笔记本”也认
GAMING_SERIES = (
    "拯救者", "r7000", "r9000", "y7000", "y9000", "legion", "geekpro",
    "天选", "飞行堡垒", "rog", "魔霸", "枪神", "幻14", "幻16",
    "暗影精灵", "光影精灵", "omen", "游匣", "g15", "g16", "外星人", "alienware",
    "暗影骑士", "掠夺者", "nitro", "predator",
    "蛟龙", "旷世", "极光", "钛钽", "将星", "隐星", "战神", "炫龙", "雷神", "机械师",
    "未来人类", "火影", "ge62", "ge75", "gp75", "gl75", "ge66", "gs66", "msi",
)

# 明确只卖配件的标题：即使带上机型或显卡词也直接丢；「送/带/含/附/赠」开头的赠品除外
ACCESSORY_STRONG = (
    "电脑包", "内胆包", "双肩包", "鼠标垫", "散热底座", "硬盘盒", "键盘膜",
    "保护壳", "保护套", "屏幕总成", "贴膜", "贴纸", "防尘塞",
    "配件机", "当配件", "配件出", "配件价", "配件专用", "拆机配件",
    "只出配件", "单出配件", "卖配件",
)

# 配件和整机都可能出现的词：标题里没有整机线索时才丢
ACCESSORY_WEAK = (
    "电源适配器", "充电器", "充电宝", "移动电源", "数据线", "充电线",
    "电源线", "鼠标", "键盘", "背包", "支架", "散热器", "底座", "托架",
    "腕托", "掌托", "扩展坞", "拓展坞", "内存条", "固态硬盘", "移动硬盘",
    "u盘", "读卡器", "耳机", "音箱", "手写笔", "触控笔", "转接头",
    "转换器", "网线", "显示器", "电脑桌", "电脑椅",
)

GIFT_PREFIXES = ("送", "带", "含", "附", "赠")
CONTRAST_PREFIXES = ("比", "不如", "胜过", "优于", "不是", "并非")
_PREFIX_SPLIT = re.compile(r"[，,。；;、！!？?|/\s]+")

# N95（Alder Lake-N）是迷你主机/软路由的 U，正经游戏本不会用；
# 边界判断放过 N950/N950KP 等机型编号，赠品「N95 口罩」也不算。
_REJECT_CPU = re.compile(r"(?<![a-z0-9])n95(?![0-9a-z])")


def _spec_adjust(title: str, ref: float) -> tuple[float, list[str]]:
    """按标题里的内存/硬盘容量对行情表价格做加减（只在有明确上下文时生效）。"""
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
    fatal: bool = False


def _near_word(text: str, index: int, words: tuple[str, ...], width: int = 6) -> str:
    segment = _PREFIX_SPLIT.split(text[max(0, index - width) : index])[-1]
    return next((word for word in words if word in segment), "")


def _ungifted_hit(title: str, words: tuple[str, ...]) -> str:
    lowered = title.casefold()
    for word in words:
        needle = word.casefold()
        if not hit_words(title, [word]):
            continue
        index = lowered.find(needle)
        while index >= 0:
            if not _near_word(lowered, index, GIFT_PREFIXES):
                return word
            index = lowered.find(needle, index + len(needle))
    return ""


def _accessory_hit(title: str) -> str:
    strong = _ungifted_hit(title, ACCESSORY_STRONG)
    if strong:
        return strong
    lowered = title.casefold()
    has_laptop = any(word.casefold() in lowered for word in LAPTOP_WORDS) or any(
        series in lowered for series in GAMING_SERIES
    )
    if has_laptop:
        return ""
    return _ungifted_hit(title, ACCESSORY_WEAK)


def _hard_exclude_hit(title: str) -> str:
    lowered = title.casefold()
    for word in HARD_EXCLUDE:
        needle = word.casefold()
        if not hit_words(title, [word]):
            continue
        index = lowered.find(needle)
        while index >= 0:
            if not _near_word(lowered, index, CONTRAST_PREFIXES, width=3):
                return word
            index = lowered.find(needle, index + len(needle))
    return _accessory_hit(title)


def _capacity_allowed(text: str, start: int) -> bool:
    """「最大支持 32G」是能力值，「已升级到 16G」才是实际容量。"""
    prefix = text[max(0, start - 24) : start]
    cut = 0
    # 只看当前这半句、当前这个容量之后的部分，避免上一句话的「支持」窜进来。
    for pattern in (r"[，,。；;、！!？?|/\n（）()【】\[\]]", r"\d+\s*[gt]"):
        for match in re.finditer(pattern, prefix):
            cut = max(cut, match.end())
    segment = prefix[cut:]
    if any(word in segment for word in _UPGRADE_PREFIXES):
        return True
    return not any(word in segment for word in _CAPABILITY_WORDS)


def _upgrade_disk_context(text: str, match: re.Match) -> bool:
    before = text[max(0, match.start() - 8) : match.start()]
    after = text[match.end() : match.end() + 4]
    return any(word in before or word in after for word in _DISK_NOUNS)


def _ram_gb(text: str) -> list[int]:
    """解析内存容量；只认明确的内存上下文或 4G*2 / 4G+4G 这类成对写法。"""
    values: list[int] = []

    def add(number: int) -> None:
        if 0 < number <= 256:
            values.append(number)

    for match in re.finditer(
        rf"{_RAM_CONTEXT}\s*[:：]?\s*(\d{{1,2}})\s*g(?:\s*[*×x]\s*(\d))?", text
    ):
        if _capacity_allowed(text, match.start()):
            add(int(match.group(1)) * (int(match.group(2)) if match.group(2) else 1))
    for match in re.finditer(
        rf"(?<![\d.])(\d{{1,2}})\s*g\s*(?:运行内存|内存|ram|ddr[45]?)", text
    ):
        if _capacity_allowed(text, match.start()):
            add(int(match.group(1)))
    # 成对写法本身就是内存特征（显卡不会 4G*2 / 4G+4G）。
    for match in re.finditer(r"(?<![\d.])(\d{1,2})\s*g\s*[*×x]\s*(\d)", text):
        if _capacity_allowed(text, match.start()):
            add(int(match.group(1)) * int(match.group(2)))
    for match in re.finditer(r"(?<![\d.])(\d{1,2})\s*g\s*\+\s*(\d{1,2})\s*g(?!\w)", text):
        if _capacity_allowed(text, match.start()):
            add(int(match.group(1)) + int(match.group(2)))
    for match in _UPGRADE_VALUE.finditer(text):
        value = int(match.group(1))
        if match.group(2) == "t" or value >= 120 or _upgrade_disk_context(text, match):
            continue
        add(value)
    return values


def _disk_gb(text: str) -> list[int]:
    """解析硬盘容量；小容量只认明确上下文，避免把显存 4G/6G 当成硬盘。"""
    values: list[int] = []

    def add(number: int) -> None:
        if 0 < number <= 8192:
            values.append(number)

    for match in re.finditer(rf"{_DISK_CONTEXT}\s*[:：]?\s*(\d{{2,4}})\s*g", text):
        if _capacity_allowed(text, match.start()):
            add(int(match.group(1)))
    for match in re.finditer(rf"(?<![\d.])(\d{{2,4}})\s*g\s*{_DISK_CONTEXT}", text):
        if _capacity_allowed(text, match.start()):
            add(int(match.group(1)))
    for match in re.finditer(rf"{_DISK_CONTEXT}\s*[:：]?\s*([12])\s*t(?:b)?", text):
        if _capacity_allowed(text, match.start()):
            add(int(match.group(1)) * 1024)
    for match in re.finditer(rf"(?<![\d.])([12])\s*t(?:b)?\s*{_DISK_CONTEXT}", text):
        if _capacity_allowed(text, match.start()):
            add(int(match.group(1)) * 1024)
    for match in re.finditer(r"(?<![\d.])([12])\s*t(?:b)?(?![a-z])", text):
        if _capacity_allowed(text, match.start()):
            add(int(match.group(1)) * 1024)
    # 没有上下文的 128G/512G 基本只会是硬盘，显存不会到这个量级。
    for match in re.finditer(r"(?<![\d.])(\d{3,4})\s*g(?![a-z])", text):
        if _capacity_allowed(text, match.start()):
            add(int(match.group(1)))
    for match in _UPGRADE_VALUE.finditer(text):
        value = int(match.group(1))
        if match.group(2) == "t":
            add(value * 1024)
        elif value >= 120 or _upgrade_disk_context(text, match):
            add(value)
    return values


def _rejected_cpu(text: str) -> str:
    lowered = text.casefold()
    for match in _REJECT_CPU.finditer(lowered):
        prefix = lowered[max(0, match.start() - 8) : match.start()]
        prefix = _PREFIX_SPLIT.sub(" ", prefix).strip()
        if any(word in prefix for word in ("不是", "并非", "非")):
            continue
        if lowered[match.end() :].lstrip().startswith("口罩"):
            continue
        return "N95"
    return ""


def _fatal_reject_reason(listing: Listing, cfg: Config) -> str:
    """商家/故障硬规则：命中就不进入打分和报表。"""
    text = f"{listing.title}\n{listing.desc or ''}".casefold()
    cpu = _rejected_cpu(text)
    if cpu:
        return f"处理器 {cpu}，直接排除"

    missing = hit_words(
        text,
        cfg.signals.merchant_reject,
        blocked_tails=REJECT_BLOCKED_TAILS,
    )
    if missing:
        return "疑似商家：空机出售" if "空机" in missing else "疑似商家：缺少内存/硬盘"

    ram = max(_ram_gb(text), default=0)
    if 0 < ram < FATAL_RAM_GB:
        return f"疑似商家：低配（内存 {ram}G）"

    disk = max(_disk_gb(text), default=0)
    if 0 < disk < FATAL_DISK_GB:
        return f"疑似商家：低配（硬盘 {disk}G）"

    boot = hit_words(text, cfg.signals.boot_reject)
    if boot:
        return f"开机故障：{boot[0]}"

    merchant = hit_words(listing.title, cfg.signals.merchant)
    seller = (listing.seller or "").casefold()
    seller_hit = next(
        (word for word in cfg.filters.seller_exclude if word and word.casefold() in seller),
        "",
    )
    if merchant or seller_hit:
        hits = [*merchant]
        if seller_hit and seller_hit not in hits:
            hits.append(seller_hit)
        return "疑似商家：" + "、".join(hits[:2])
    return ""


def gate(listing: Listing, cfg: Config) -> Gate:
    title = listing.title
    fatal = _fatal_reject_reason(listing, cfg)
    if fatal:
        brand = brands.detect(title, listing.seller)
        return Gate(False, fatal, None, brand, fatal=True)

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

    # 硬排除：工作站/台式/求购/配件。否定句（无拆机）和对比句（比台式机轻）放行
    hard = _hard_exclude_hit(title)
    if hard:
        return Gate(False, f"含排除词「{hard}」，不是整机游戏本", gpu, brand)

    # 软排除：轻薄/商务/办公等，标题写了“游戏本”时允许豁免（卖家常叠词）
    if hit_words(title, cfg.category.exclude):
        if "游戏本" not in lowered:
            return Gate(False, "标题显示是轻薄本/商务本/台式机", gpu, brand)
    if any(word.casefold() in lowered for word in DESKTOP_HINTS):
        return Gate(False, "台式机显卡/配件，不是整机", gpu, brand)
    # 台式显卡的描述特征：显卡 + 挡板/插上/这张 等
    if "显卡" in lowered and any(
        word in lowered for word in ("挡板", "插上", "这张", "8pin", "6pin", "矿卡", "单卡", "显卡坞")
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
            fatal=result.fatal,
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
    if hit_words(listing.title, cfg.filters.exclude):
        return False
    return True


def is_fresh(listing: Listing, max_age_hours: float, now_ms: int) -> bool:
    if max_age_hours <= 0 or listing.publish_ts <= 0:
        return True
    return (now_ms - listing.publish_ts) / 3_600_000 <= max_age_hours