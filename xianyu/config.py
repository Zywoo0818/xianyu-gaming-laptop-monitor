"""配置加载与校验。所有用户可调参数都在 config.yaml 里。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """配置文件有误时抛出，消息直接展示给用户。"""


def _as_float(value: Any, default: float, label: str) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} 必须是数字，当前是 {value!r}") from exc


def _as_int(value: Any, default: int, label: str, minimum: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} 必须是整数，当前是 {value!r}") from exc
    if number < minimum:
        raise ConfigError(f"{label} 不能小于 {minimum}")
    return number


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_str_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
    if not isinstance(value, list):
        raise ConfigError(f"{label} 必须是列表，例如 [a, b]")
    return [str(item).strip() for item in value if str(item).strip()]


def _as_str_list_or_default(
    value: Any, default: tuple[str, ...] | list[str], label: str
) -> list[str]:
    """没写（None）才吃默认词表；显式写 [] 表示关掉，不能被默认值悄悄顶掉。"""
    if value is None:
        return list(default)
    return _as_str_list(value, label)


WATCH_KEYS = {"name", "keyword", "reference", "min_price", "max_price", "pages"}

DEFAULT_MERCHANT_REJECT = (
    "空机",
    "无内存", "没内存", "没有内存", "不含内存", "缺内存",
    "无硬盘", "没硬盘", "没有硬盘", "不含硬盘", "缺硬盘",
    "无固态", "没有固态", "不含固态",
    "内存硬盘都没有", "内存硬盘全无", "内存硬盘均无",
)
DEFAULT_BOOT_REJECT = ("无法开机", "开不了机", "不能开机", "不开机", "点不亮")
# 问题机/故障描述（v3.8.4 主人新增）：标题/介绍命中即排除
DEFAULT_PROBLEM_REJECT = ("问题机", "进不去系统", "win转圈卡死", "处理配件")
DEFAULT_SELLER_EXCLUDE = (
    "店铺", "实体店", "批发", "回收", "工作室", "数码城", "专营店", "数码",
    "小店", "精选", "小卖部", "购物", "优品", "笔记本",
    # v3.8.4 主人新增
    "经销部", "电脑", "严选", "电子", "包换", "二手", "科技", "小铺",
)
DEFAULT_FINE_PART_WORDS = (
    "内存", "内存条", "硬盘", "固态", "ssd", "nvme", "显卡", "主板",
    "屏幕", "屏线", "排线", "键盘", "电池", "电源", "适配器", "充电器",
    "散热", "风扇", "外壳", "触控板", "处理器", "cpu", "电脑配件", "配件",
)
DEFAULT_DJI_PATTERNS = ("大疆", r"(?<![A-Za-z])D\s*J\s*I(?![A-Za-z])")
DEFAULT_CATEGORY_EXCLUDE = (
    "轻薄", "超极本", "商务本", "办公本", "学生本", "二合一", "平板",
    "上网本", "全能本", "创作本", "设计本",
)
DEFAULT_REVIEW_PORT = 8765


@dataclass
class Watch:
    name: str
    keyword: str
    reference: float = 0
    min_price: float = 0
    max_price: float = 1e9
    pages: int = 2


@dataclass
class BrowserConfig:
    profile_dir: Path
    channel: str | None = "chrome"
    headless: bool = False
    timeout_seconds: int = 45
    min_delay: float = 8.0
    max_delay: float = 15.0
    user_agent: str = ""
    require_login: bool = True
    stealth: bool = True
    # 有头模式遇到 Baxia 滑块时等主人手动处理的秒数
    baxia_wait_seconds: int = 120


@dataclass
class Filters:
    exclude: list[str] = field(default_factory=list)
    seller_exclude: list[str] = field(default_factory=list)
    min_price: float = 0
    max_price: float = 1e9
    location_allow: list[str] = field(default_factory=list)


@dataclass
class Signals:
    good: list[str] = field(default_factory=list)
    bad: list[str] = field(default_factory=list)
    merchant: list[str] = field(default_factory=list)
    bait: list[str] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    merchant_reject: list[str] = field(default_factory=lambda: list(DEFAULT_MERCHANT_REJECT))
    boot_reject: list[str] = field(default_factory=lambda: list(DEFAULT_BOOT_REJECT))
    problem_reject: list[str] = field(default_factory=lambda: list(DEFAULT_PROBLEM_REJECT))


@dataclass
class ScanConfig:
    pages: int = 2
    # 客户端发布时间窗（小时）：0 = 不筛。点「新发布 → 最新」拿真时间序后，
    # 24 小时窗口由这里在粗筛前过滤（engine 在粗筛前调 is_fresh，不受 pricing 开关影响）。
    max_age_hours: float = 24
    min_score: float = 55
    min_gap: float = 100
    top_n_markdown: int = 50
    # 进搜索页后点「新发布 → 最新」；URL 参数 sortType 实测被服务端忽略
    newest_first: bool = True
    # 整页都是库里的老货且确认新→旧时，不再往下翻页
    stop_on_known: bool = True
    # D2 卖家画像：评价数达到该值就在审核页/报表标注「疑似职业卖家」。
    # 只标注、不排除（样本量还不够拍硬阈值）；0 = 关闭标注。
    seller_review_hint: int = 100
    failure_threshold: int = 3
    # 兼容保留：细筛已整体移除，细筛风控计数恒为 0，该熔断不会触发
    fine_failure_threshold: int = 3
    rate_limit_threshold: int = 1
    soft_failure_threshold: int = 3
    cooldown_minutes: int = 1440
    cooldown_max_minutes: int = 10080
    rate_limit_cooldown_minutes: int = 30
    rate_limit_max_minutes: int = 720
    soft_cooldown_minutes: int = 60
    soft_cooldown_max_minutes: int = 1440
    daily_request_budget: int = 200


@dataclass
class FineConfig:
    """方案二不再打开卖家主页；保留配置结构仅为兼容旧配置文件。"""

    enabled: bool = False
    item_limit: int = 20
    merchant_min_items: int = 5
    dji_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_DJI_PATTERNS))
    part_words: list[str] = field(default_factory=lambda: list(DEFAULT_FINE_PART_WORDS))
    cache_hours: float = 24
    min_delay: float = 3.0
    max_delay: float = 6.0
    wait_seconds: int = 15
    max_per_round: int = 0


@dataclass
class LoopConfig:
    reuse_browser: bool = True
    # 相邻两轮的最小间隔；提前输入 next 会要求再确认一次
    min_round_gap_seconds: int = 300


@dataclass
class CategoryConfig:
    exclude: list[str] = field(default_factory=list)


@dataclass
class ScoringConfig:
    base: float = 20
    price_max: float = 40
    wants_penalty_low: float = -4
    wants_penalty_high: float = -8


@dataclass
class PricingConfig:
    enabled: bool = False


@dataclass
class ReviewConfig:
    """本地审核页：报表「忽略」键和 review 命令共用这个端口。"""

    port: int = DEFAULT_REVIEW_PORT


@dataclass
class PriceTableConfig:
    path: Path
    source_xlsx: str = ""


@dataclass
class NotifyConfig:
    wecom_webhook: str = ""
    bark_url: str = ""
    min_score: float = 0

    @property
    def enabled(self) -> bool:
        return bool(self.wecom_webhook or self.bark_url)


@dataclass
class Config:
    path: Path
    root: Path
    browser: BrowserConfig
    filters: Filters
    signals: Signals
    scan: ScanConfig
    fine: FineConfig
    loop: LoopConfig
    notify: NotifyConfig
    watches: list[Watch]
    category: CategoryConfig = field(default_factory=CategoryConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    price_table: PriceTableConfig | None = None

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "xianyu.db"

    def watch(self, name: str) -> Watch:
        for item in self.watches:
            if item.name.casefold() == name.casefold():
                return item
        raise ConfigError(f"没有找到名为 {name!r} 的监控项，可用：{[w.name for w in self.watches]}")

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        cfg_path = Path(path or os.getenv("XIANYU_CONFIG") or "config.yaml").expanduser()
        if not cfg_path.is_file():
            raise ConfigError(f"找不到配置文件：{cfg_path.resolve()}")
        root = cfg_path.resolve().parent
        try:
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"配置文件不是合法的 YAML：{exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("配置文件最外层必须是键值对")

        browser_raw = raw.get("browser") or {}
        profile = Path(str(browser_raw.get("profile_dir") or "data/browser_profile"))
        if not profile.is_absolute():
            profile = root / profile
        channel = browser_raw.get("channel", "chrome")
        browser = BrowserConfig(
            profile_dir=profile,
            channel=(str(channel).strip() or None) if channel else None,
            headless=_as_bool(browser_raw.get("headless"), False),
            timeout_seconds=_as_int(browser_raw.get("timeout_seconds"), 45, "browser.timeout_seconds", 10),
            min_delay=_as_float(browser_raw.get("min_delay"), 8.0, "browser.min_delay"),
            max_delay=_as_float(browser_raw.get("max_delay"), 15.0, "browser.max_delay"),
            user_agent=str(browser_raw.get("user_agent") or "").strip(),
            require_login=_as_bool(browser_raw.get("require_login"), True),
            stealth=_as_bool(browser_raw.get("stealth"), True),
            baxia_wait_seconds=_as_int(
                browser_raw.get("baxia_wait_seconds"), 120, "browser.baxia_wait_seconds", 10
            ),
        )
        if browser.max_delay < browser.min_delay:
            browser.max_delay = browser.min_delay

        filters_raw = raw.get("filters") or {}
        filters = Filters(
            exclude=_as_str_list(filters_raw.get("exclude"), "filters.exclude"),
            seller_exclude=_as_str_list_or_default(
                filters_raw.get("seller_exclude"),
                DEFAULT_SELLER_EXCLUDE,
                "filters.seller_exclude",
            ),
            min_price=_as_float(filters_raw.get("min_price"), 0, "filters.min_price"),
            max_price=_as_float(filters_raw.get("max_price"), 1e9, "filters.max_price"),
            location_allow=_as_str_list(filters_raw.get("location_allow"), "filters.location_allow"),
        )

        signals_raw = raw.get("signals") or {}
        signals = Signals(
            good=_as_str_list(signals_raw.get("good"), "signals.good"),
            bad=_as_str_list(signals_raw.get("bad"), "signals.bad"),
            merchant=_as_str_list(signals_raw.get("merchant"), "signals.merchant"),
            bait=_as_str_list(signals_raw.get("bait"), "signals.bait"),
            broken=_as_str_list(signals_raw.get("broken"), "signals.broken"),
            merchant_reject=_as_str_list_or_default(
                signals_raw.get("merchant_reject"),
                DEFAULT_MERCHANT_REJECT,
                "signals.merchant_reject",
            ),
            boot_reject=_as_str_list_or_default(
                signals_raw.get("boot_reject"),
                DEFAULT_BOOT_REJECT,
                "signals.boot_reject",
            ),
            problem_reject=_as_str_list_or_default(
                signals_raw.get("problem_reject"),
                DEFAULT_PROBLEM_REJECT,
                "signals.problem_reject",
            ),
        )

        scan_raw = raw.get("scan") or {}
        scan = ScanConfig(
            pages=_as_int(scan_raw.get("pages"), 2, "scan.pages", 1),
            max_age_hours=_as_float(scan_raw.get("max_age_hours"), 24, "scan.max_age_hours"),
            min_score=_as_float(scan_raw.get("min_score"), 55, "scan.min_score"),
            min_gap=_as_float(scan_raw.get("min_gap"), 100, "scan.min_gap"),
            top_n_markdown=_as_int(scan_raw.get("top_n_markdown"), 50, "scan.top_n_markdown", 1),
            newest_first=_as_bool(scan_raw.get("newest_first"), True),
            stop_on_known=_as_bool(scan_raw.get("stop_on_known"), True),
            seller_review_hint=_as_int(
                scan_raw.get("seller_review_hint"), 100, "scan.seller_review_hint", 0
            ),
            failure_threshold=_as_int(
                scan_raw.get("failure_threshold"), 3, "scan.failure_threshold", 0
            ),
            fine_failure_threshold=_as_int(
                scan_raw.get("fine_failure_threshold"), 3, "scan.fine_failure_threshold", 0
            ),
            rate_limit_threshold=_as_int(
                scan_raw.get("rate_limit_threshold"), 1, "scan.rate_limit_threshold", 0
            ),
            soft_failure_threshold=_as_int(
                scan_raw.get("soft_failure_threshold"), 3, "scan.soft_failure_threshold", 0
            ),
            cooldown_minutes=_as_int(
                scan_raw.get("cooldown_minutes"), 1440, "scan.cooldown_minutes", 1
            ),
            cooldown_max_minutes=_as_int(
                scan_raw.get("cooldown_max_minutes"), 10080, "scan.cooldown_max_minutes", 1
            ),
            rate_limit_cooldown_minutes=_as_int(
                scan_raw.get("rate_limit_cooldown_minutes"),
                30,
                "scan.rate_limit_cooldown_minutes",
                1,
            ),
            rate_limit_max_minutes=_as_int(
                scan_raw.get("rate_limit_max_minutes"), 720, "scan.rate_limit_max_minutes", 1
            ),
            soft_cooldown_minutes=_as_int(
                scan_raw.get("soft_cooldown_minutes"), 60, "scan.soft_cooldown_minutes", 1
            ),
            soft_cooldown_max_minutes=_as_int(
                scan_raw.get("soft_cooldown_max_minutes"),
                1440,
                "scan.soft_cooldown_max_minutes",
                1,
            ),
            daily_request_budget=_as_int(
                scan_raw.get("daily_request_budget"), 200, "scan.daily_request_budget", 0
            ),
        )

        fine_raw = raw.get("fine") or {}
        dji_patterns = _as_str_list_or_default(
            fine_raw.get("dji_patterns"), DEFAULT_DJI_PATTERNS, "fine.dji_patterns"
        )
        for pattern in dji_patterns:
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ConfigError(
                    f"fine.dji_patterns 里的正则无效：{pattern!r}（{exc}）"
                ) from exc
        fine = FineConfig(
            enabled=_as_bool(fine_raw.get("enabled"), False),
            item_limit=_as_int(fine_raw.get("item_limit"), 20, "fine.item_limit", 1),
            merchant_min_items=_as_int(
                fine_raw.get("merchant_min_items"), 5, "fine.merchant_min_items", 1
            ),
            dji_patterns=dji_patterns,
            part_words=_as_str_list_or_default(
                fine_raw.get("part_words"), DEFAULT_FINE_PART_WORDS, "fine.part_words"
            ),
            cache_hours=_as_float(fine_raw.get("cache_hours"), 24, "fine.cache_hours"),
            min_delay=_as_float(fine_raw.get("min_delay"), 3.0, "fine.min_delay"),
            max_delay=_as_float(fine_raw.get("max_delay"), 6.0, "fine.max_delay"),
            wait_seconds=_as_int(fine_raw.get("wait_seconds"), 15, "fine.wait_seconds", 3),
            max_per_round=_as_int(fine_raw.get("max_per_round"), 0, "fine.max_per_round", 0),
        )
        if fine.max_delay < fine.min_delay:
            fine.max_delay = fine.min_delay

        loop_raw = raw.get("loop") or {}
        loop = LoopConfig(
            reuse_browser=_as_bool(loop_raw.get("reuse_browser"), True),
            min_round_gap_seconds=_as_int(
                loop_raw.get("min_round_gap_seconds"),
                300,
                "loop.min_round_gap_seconds",
                0,
            ),
        )

        category_raw = raw.get("category") or {}
        category = CategoryConfig(
            exclude=_as_str_list_or_default(
                category_raw.get("exclude"), DEFAULT_CATEGORY_EXCLUDE, "category.exclude"
            )
        )

        scoring_raw = raw.get("scoring") or {}
        scoring = ScoringConfig(
            base=_as_float(scoring_raw.get("base"), 20, "scoring.base"),
            price_max=_as_float(scoring_raw.get("price_max"), 40, "scoring.price_max"),
            wants_penalty_low=_as_float(scoring_raw.get("wants_penalty_low"), -4, "scoring.wants_penalty_low"),
            wants_penalty_high=_as_float(scoring_raw.get("wants_penalty_high"), -8, "scoring.wants_penalty_high"),
        )

        pricing_raw = raw.get("pricing") or {}
        pricing = PricingConfig(enabled=_as_bool(pricing_raw.get("enabled"), False))

        review_raw = raw.get("review") or {}
        review = ReviewConfig(
            port=_as_int(
                review_raw.get("port"), DEFAULT_REVIEW_PORT, "review.port", 1
            )
        )

        table_raw = raw.get("price_table") or {}
        table_path = Path(str(table_raw.get("path") or "data/price_table.json"))
        if not table_path.is_absolute():
            table_path = root / table_path
        price_table = PriceTableConfig(path=table_path, source_xlsx=str(table_raw.get("source_xlsx") or ""))

        notify_raw = raw.get("notify") or {}
        notify = NotifyConfig(
            wecom_webhook=str(notify_raw.get("wecom_webhook") or "").strip(),
            bark_url=str(notify_raw.get("bark_url") or "").strip(),
            min_score=_as_float(notify_raw.get("min_score"), 0, "notify.min_score"),
        )

        watches_raw = raw.get("watches") or []
        if not isinstance(watches_raw, list) or not watches_raw:
            raise ConfigError("watches 不能为空，至少要有一个监控关键词")
        watches: list[Watch] = []
        names: set[str] = set()
        for index, item in enumerate(watches_raw, start=1):
            if not isinstance(item, dict):
                raise ConfigError(f"watches 第 {index} 项必须是键值对")
            keyword = str(item.get("keyword") or "").strip()
            if not keyword:
                raise ConfigError(f"watches 第 {index} 项缺少 keyword")
            name = str(item.get("name") or keyword).strip()
            if name in names:
                raise ConfigError(f"监控项名称重复：{name}")
            names.add(name)
            unknown = sorted(set(item) - WATCH_KEYS)
            if unknown:
                raise ConfigError(
                    f"{name} 有不认识的配置项 {unknown}；watch 只支持 "
                    f"{sorted(WATCH_KEYS)}，写了也不会生效"
                )
            watch = Watch(
                name=name,
                keyword=keyword,
                reference=_as_float(item.get("reference"), 0, f"{name}.reference"),
                min_price=_as_float(item.get("min_price"), filters.min_price, f"{name}.min_price"),
                max_price=_as_float(item.get("max_price"), filters.max_price, f"{name}.max_price"),
                pages=_as_int(item.get("pages"), scan.pages, f"{name}.pages", 1),
            )
            if watch.max_price <= watch.min_price:
                raise ConfigError(f"{name} 的 max_price 必须大于 min_price")
            watches.append(watch)

        return cls(
            path=cfg_path,
            root=root,
            browser=browser,
            filters=filters,
            signals=signals,
            scan=scan,
            fine=fine,
            loop=loop,
            notify=notify,
            watches=watches,
            category=category,
            scoring=scoring,
            pricing=pricing,
            review=review,
            price_table=price_table,
        )

    def ensure_dirs(self) -> None:
        for directory in (self.data_dir, self.reports_dir, self.browser.profile_dir.parent):
            directory.mkdir(parents=True, exist_ok=True)
