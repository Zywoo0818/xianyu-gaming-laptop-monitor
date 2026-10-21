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


WATCH_KEYS = {"name", "keyword", "reference", "min_price", "max_price", "pages"}

DEFAULT_MERCHANT_REJECT = (
    "空机",
    "无内存", "没内存", "没有内存", "不含内存", "缺内存",
    "无硬盘", "没硬盘", "没有硬盘", "不含硬盘", "缺硬盘",
    "无固态", "没有固态", "不含固态",
    "内存硬盘都没有", "内存硬盘全无", "内存硬盘均无",
)
DEFAULT_BOOT_REJECT = ("无法开机", "开不了机", "不能开机", "不开机", "点不亮")


@dataclass
class Watch:
    name: str
    keyword: str
    reference: float = 0
    min_price: float = 500
    max_price: float = 8000
    pages: int = 2


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


@dataclass
class ScanConfig:
    pages: int = 2
    max_age_hours: float = 0
    min_score: float = 55
    min_gap: float = 100
    top_n_markdown: int = 20
    detail_limit: int = 5
    failure_threshold: int = 3
    detail_failure_threshold: int = 3
    soft_failure_threshold: int = 3
    cooldown_minutes: int = 1440
    cooldown_max_minutes: int = 10080
    soft_cooldown_minutes: int = 60
    soft_cooldown_max_minutes: int = 1440
    login_quiet_minutes: int = 60


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
class PriceTableConfig:
    path: Path
    source_xlsx: str = ""


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
    filters: Filters
    signals: Signals
    scan: ScanConfig
    notify: NotifyConfig
    watches: list[Watch]
    category: CategoryConfig = field(default_factory=CategoryConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
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
            merchant_reject=(
                _as_str_list(signals_raw.get("merchant_reject"), "signals.merchant_reject")
                or list(DEFAULT_MERCHANT_REJECT)
            ),
            boot_reject=(
                _as_str_list(signals_raw.get("boot_reject"), "signals.boot_reject")
                or list(DEFAULT_BOOT_REJECT)
            ),
        )

        scan_raw = raw.get("scan") or {}
        scan = ScanConfig(
            pages=_as_int(scan_raw.get("pages"), 2, "scan.pages", 1),
            max_age_hours=_as_float(scan_raw.get("max_age_hours"), 0, "scan.max_age_hours"),
            min_score=_as_float(scan_raw.get("min_score"), 55, "scan.min_score"),
            min_gap=_as_float(scan_raw.get("min_gap"), 100, "scan.min_gap"),
            top_n_markdown=_as_int(scan_raw.get("top_n_markdown"), 20, "scan.top_n_markdown", 1),
            detail_limit=_as_int(scan_raw.get("detail_limit"), 5, "scan.detail_limit", 0),
            failure_threshold=_as_int(
                scan_raw.get("failure_threshold"), 3, "scan.failure_threshold", 0
            ),
            detail_failure_threshold=_as_int(
                scan_raw.get("detail_failure_threshold"),
                3,
                "scan.detail_failure_threshold",
                0,
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
            soft_cooldown_minutes=_as_int(
                scan_raw.get("soft_cooldown_minutes"), 60, "scan.soft_cooldown_minutes", 1
            ),
            soft_cooldown_max_minutes=_as_int(
                scan_raw.get("soft_cooldown_max_minutes"),
                1440,
                "scan.soft_cooldown_max_minutes",
                1,
            ),
            login_quiet_minutes=_as_int(
                scan_raw.get("login_quiet_minutes"), 60, "scan.login_quiet_minutes", 1
            ),
        )

        category_raw = raw.get("category") or {}
        category = CategoryConfig(
            exclude=_as_str_list(category_raw.get("exclude"), "category.exclude")
            or [
                "轻薄", "超极本", "商务本", "办公本", "学生本", "二合一", "平板",
                "上网本", "全能本", "创作本", "设计本",
            ]
        )

        scoring_raw = raw.get("scoring") or {}
        scoring = ScoringConfig(
            base=_as_float(scoring_raw.get("base"), 20, "scoring.base"),
            price_max=_as_float(scoring_raw.get("price_max"), 40, "scoring.price_max"),
            wants_penalty_low=_as_float(scoring_raw.get("wants_penalty_low"), -4, "scoring.wants_penalty_low"),
            wants_penalty_high=_as_float(scoring_raw.get("wants_penalty_high"), -8, "scoring.wants_penalty_high"),
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
            notify=notify,
            watches=watches,
            category=category,
            scoring=scoring,
            price_table=price_table,
        )

    def ensure_dirs(self) -> None:
        for directory in (self.data_dir, self.reports_dir, self.browser.profile_dir.parent):
            directory.mkdir(parents=True, exist_ok=True)
