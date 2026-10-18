"""配置加载与校验。所有用户可调参数都在 config.yaml 里。"""
from __future__ import annotations

import os
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


def _as_str_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
    if not isinstance(value, list):
        raise ConfigError(f"{label} 必须是列表，例如 [a, b]")
    return [str(item).strip() for item in value if str(item).strip()]


@dataclass
class SpecRule:
    pattern: str
    add: float
    label: str = ""

    def match(self, title: str) -> bool:
        try:
            import re

            return re.search(self.pattern, title, re.IGNORECASE) is not None
        except re.error:
            return self.pattern.lower() in title.lower()

    @property
    def name(self) -> str:
        return self.label or self.pattern


@dataclass
class Watch:
    name: str
    keyword: str
    reference: float = 0
    min_price: float = 500
    max_price: float = 8000
    pages: int = 2
    exclude: list[str] = field(default_factory=list)
    location: str = ""
    specs: list[SpecRule] = field(default_factory=list)

    def excluded(self, title: str, extra: list[str]) -> str | None:
        lowered = title.casefold()
        for word in [*extra, *self.exclude]:
            if word and word.casefold() in lowered:
                return word
        return None


@dataclass
class BrowserConfig:
    profile_dir: Path
    channel: str | None = "chrome"
    headless: bool = True
    headful_fallback: bool = True
    timeout_seconds: int = 45
    min_delay: float = 3.0
    max_delay: float = 7.0
    user_agent: str = ""
    require_login: bool = True


@dataclass
class MarketConfig:
    mode: str = "blend"
    blend_weight: float = 0.65
    min_samples: int = 5
    target_factor: float = 0.82
    floor_factor: float = 0.45

    def validate(self) -> None:
        if self.mode not in {"auto", "config", "blend"}:
            raise ConfigError("market.mode 只能是 auto / config / blend")
        if not 0 < self.blend_weight <= 1:
            raise ConfigError("market.blend_weight 必须在 0~1 之间")
        if not 0 < self.floor_factor < self.target_factor < 1.5:
            raise ConfigError("market 里必须满足 0 < floor_factor < target_factor")
        if self.min_samples < 1:
            raise ConfigError("market.min_samples 至少为 1")


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


@dataclass
class ScanConfig:
    pages: int = 2
    max_age_hours: float = 0
    min_score: float = 55
    min_gap: float = 100
    top_n_markdown: int = 20


@dataclass
class CategoryConfig:
    exclude: list[str] = field(default_factory=list)


@dataclass
class ScoringConfig:
    base: float = 20
    price_max: float = 40
    merchant_penalty: float = -18
    audited_merchant_penalty: float = -10
    wants_penalty_low: float = -4
    wants_penalty_high: float = -8


@dataclass
class PriceTableConfig:
    path: Path
    source_xlsx: str = ""


@dataclass
class AuditConfig:
    enabled: bool = True
    max_items: int = 8
    min_score: float = 50
    scroll_times: int = 3


@dataclass
class NotifyConfig:
    wecom_webhook: str = ""
    bark_url: str = ""
    min_score: float = 75

    @property
    def enabled(self) -> bool:
        return bool(self.wecom_webhook or self.bark_url)


@dataclass
class Config:
    path: Path
    root: Path
    browser: BrowserConfig
    market: MarketConfig
    filters: Filters
    signals: Signals
    scan: ScanConfig
    notify: NotifyConfig
    watches: list[Watch]
    category: CategoryConfig = field(default_factory=CategoryConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    price_table: PriceTableConfig | None = None
    audit: AuditConfig = field(default_factory=AuditConfig)

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
            headless=bool(browser_raw.get("headless", True)),
            headful_fallback=bool(browser_raw.get("headful_fallback", True)),
            timeout_seconds=_as_int(browser_raw.get("timeout_seconds"), 45, "browser.timeout_seconds", 10),
            min_delay=_as_float(browser_raw.get("min_delay"), 3.0, "browser.min_delay"),
            max_delay=_as_float(browser_raw.get("max_delay"), 7.0, "browser.max_delay"),
            user_agent=str(browser_raw.get("user_agent") or "").strip(),
            require_login=bool(browser_raw.get("require_login", True)),
        )
        if browser.max_delay < browser.min_delay:
            browser.max_delay = browser.min_delay

        market_raw = raw.get("market") or {}
        market = MarketConfig(
            mode=str(market_raw.get("mode") or "blend").lower(),
            blend_weight=_as_float(market_raw.get("blend_weight"), 0.65, "market.blend_weight"),
            min_samples=_as_int(market_raw.get("min_samples"), 5, "market.min_samples", 1),
            target_factor=_as_float(market_raw.get("target_factor"), 0.82, "market.target_factor"),
            floor_factor=_as_float(market_raw.get("floor_factor"), 0.45, "market.floor_factor"),
        )
        market.validate()

        filters_raw = raw.get("filters") or {}
        filters = Filters(
            exclude=_as_str_list(filters_raw.get("exclude"), "filters.exclude"),
            seller_exclude=_as_str_list(filters_raw.get("seller_exclude"), "filters.seller_exclude"),
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
        )

        scan_raw = raw.get("scan") or {}
        scan = ScanConfig(
            pages=_as_int(scan_raw.get("pages"), 2, "scan.pages", 1),
            max_age_hours=_as_float(scan_raw.get("max_age_hours"), 0, "scan.max_age_hours"),
            min_score=_as_float(scan_raw.get("min_score"), 55, "scan.min_score"),
            min_gap=_as_float(scan_raw.get("min_gap"), 100, "scan.min_gap"),
            top_n_markdown=_as_int(scan_raw.get("top_n_markdown"), 20, "scan.top_n_markdown", 1),
        )

        category_raw = raw.get("category") or {}
        category = CategoryConfig(
            exclude=_as_str_list(category_raw.get("exclude"), "category.exclude")
            or [
                "轻薄", "超极本", "商务本", "办公本", "学生本", "二合一", "平板",
                "上网本", "全能本", "创作本", "设计本", "移动工作站", "工作站",
                "台式机", "台式", "迷你主机", "一体机", "显示器",
            ]
        )

        scoring_raw = raw.get("scoring") or {}
        scoring = ScoringConfig(
            base=_as_float(scoring_raw.get("base"), 20, "scoring.base"),
            price_max=_as_float(scoring_raw.get("price_max"), 40, "scoring.price_max"),
            merchant_penalty=_as_float(scoring_raw.get("merchant_penalty"), -18, "scoring.merchant_penalty"),
            audited_merchant_penalty=_as_float(
                scoring_raw.get("audited_merchant_penalty"), -10, "scoring.audited_merchant_penalty"
            ),
            wants_penalty_low=_as_float(scoring_raw.get("wants_penalty_low"), -4, "scoring.wants_penalty_low"),
            wants_penalty_high=_as_float(scoring_raw.get("wants_penalty_high"), -8, "scoring.wants_penalty_high"),
        )

        table_raw = raw.get("price_table") or {}
        table_path = Path(str(table_raw.get("path") or "data/price_table.json"))
        if not table_path.is_absolute():
            table_path = root / table_path
        price_table = PriceTableConfig(path=table_path, source_xlsx=str(table_raw.get("source_xlsx") or ""))

        audit_raw = raw.get("audit") or {}
        audit = AuditConfig(
            enabled=bool(audit_raw.get("enabled", True)),
            max_items=_as_int(audit_raw.get("max_items"), 8, "audit.max_items", 0),
            min_score=_as_float(audit_raw.get("min_score"), 50, "audit.min_score"),
            scroll_times=_as_int(audit_raw.get("scroll_times"), 3, "audit.scroll_times", 1),
        )

        notify_raw = raw.get("notify") or {}
        notify = NotifyConfig(
            wecom_webhook=str(notify_raw.get("wecom_webhook") or "").strip(),
            bark_url=str(notify_raw.get("bark_url") or "").strip(),
            min_score=_as_float(notify_raw.get("min_score"), 75, "notify.min_score"),
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
            specs: list[SpecRule] = []
            for spec in item.get("specs") or []:
                if not isinstance(spec, dict) or not spec.get("pattern"):
                    raise ConfigError(f"{name} 的 specs 里有一项缺少 pattern")
                specs.append(
                    SpecRule(
                        str(spec["pattern"]),
                        _as_float(spec.get("add"), 0, f"{name}.specs.add"),
                        str(spec.get("label") or "").strip(),
                    )
                )
            watch = Watch(
                name=name,
                keyword=keyword,
                reference=_as_float(item.get("reference"), 0, f"{name}.reference"),
                min_price=_as_float(item.get("min_price"), filters.min_price, f"{name}.min_price"),
                max_price=_as_float(item.get("max_price"), filters.max_price, f"{name}.max_price"),
                pages=_as_int(item.get("pages"), scan.pages, f"{name}.pages", 1),
                exclude=_as_str_list(item.get("exclude"), f"{name}.exclude"),
                location=str(item.get("location") or "").strip(),
                specs=specs,
            )
            if watch.max_price <= watch.min_price:
                raise ConfigError(f"{name} 的 max_price 必须大于 min_price")
            watches.append(watch)

        return cls(
            path=cfg_path,
            root=root,
            browser=browser,
            market=market,
            filters=filters,
            signals=signals,
            scan=scan,
            notify=notify,
            watches=watches,
            category=category,
            scoring=scoring,
            price_table=price_table,
            audit=audit,
        )

    def ensure_dirs(self) -> None:
        for directory in (self.data_dir, self.reports_dir, self.browser.profile_dir.parent):
            directory.mkdir(parents=True, exist_ok=True)
