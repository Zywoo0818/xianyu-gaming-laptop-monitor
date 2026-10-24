"""方案二轮次编排：登录态搜索 → 粗筛（搜索页商家判断）→ 待审核报表。

与 v3 的差异：
- 不再打开卖家主页：跨商品商家识别（大疆/配件阈值）整体移除，转人工审核；
- 保留 v3 的粗筛全部规则（商家词匹配标题/介绍/昵称、品牌、硬排除）；
- run_round 接受外部传入的 crawler：loop 控制台跨轮复用同一个浏览器；
- 每轮只搜 2 页；本轮结束立刻出报表，未审核的商品带进下一份报表。
价格/行情表/打分默认关闭，开启时作为额外一层筛选。
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from .budget import DailyBudget, budget_path
from .coarse import coarse_reject
from .config import Config, Watch
from .crawler import (
    CaptureTimeout,
    Crawler,
    CrawlError,
    LoginRequired,
    RateLimited,
    RiskControl,
)
from .guard import RiskGuard, classify_errors, is_risk_error
from .lock import ScanBusy, ScanLock
from .models import Analysis, Listing
from .pricetable import load_table
from .pricing import analyze, is_fresh
from .report import is_pro_seller, rejected_item, row_to_item, write_all
from .scanlog import log_scan
from .store import (
    commit,
    connect,
    finish_scan,
    ignore_pending,
    now_text,
    rows_pending,
    save_rejects,
    seen_ids,
    start_scan,
    upsert,
)


class ScanPaused(Exception):
    """风控冷却中，本轮没有执行。"""


# 超窗原因只用于展示和「不符合」明细，不据此自动忽略旧待审核（见 review 的过期沉底）
FRESHNESS_REASON = "超过发布时间限制"


@dataclass
class RoundOutcome:
    scan_id: int
    items: list[dict]
    stats: dict
    errors: list[str] = field(default_factory=list)
    started: str = ""
    elapsed: float = 0.0
    paths: dict[str, Path] = field(default_factory=dict)
    stopped: bool = False
    sort_status: str = ""

    @property
    def new_count(self) -> int:
        return sum(1 for item in self.items if item.get("is_new"))

    @property
    def recommended(self) -> list[dict]:
        return [item for item in self.items if not item["suspicious"]]


@dataclass
class _RoundData:
    collected: dict[str, list[Listing]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    coarse_kept: list[Listing] = field(default_factory=list)
    coarse_reasons: dict[str, str] = field(default_factory=dict)
    rejected: list[tuple[Listing, str]] = field(default_factory=list)
    sort_status: dict[str, str] = field(default_factory=dict)
    stopped: bool = False
    aborted: bool = False


def _now_ms() -> int:
    return int(time.time() * 1000)


def _neutral_analysis(reasons: list[str], flags: list[str]) -> Analysis:
    return Analysis(
        score=0.0,
        verdict="待审核",
        ref_price=0.0,
        ratio=1.0,
        reasons=list(reasons) or ["粗筛通过"],
        flags=list(flags),
    )


def _sort_text(cfg: Config, data: _RoundData) -> str:
    if not cfg.scan.newest_first:
        return "已关闭"
    if not data.sort_status:
        return "未记录"
    return "；".join(f"{name}：{status}" for name, status in data.sort_status.items())


def classify_round(
    watches: list[Watch],
    collected: dict[str, list[Listing]],
    errors: dict[str, str],
    *,
    stopped: bool = False,
) -> tuple[bool, dict[str, list[str]]]:
    """返回 (是否完全干净, 搜索风控/软失败分组)。"""
    groups = classify_errors(errors)
    if not errors and not any(collected.values()) and not stopped:
        groups["soft"].append("全部关键词返回 0 条")
    clean = not errors and not stopped and bool(any(collected.values()))
    return clean, groups


def _coarse_filter(
    cfg: Config,
    collected: dict[str, list[Listing]],
    now_ms: int,
    logger: Callable[[str], None],
) -> tuple[list[Listing], dict[str, str], list[tuple[Listing, str]]]:
    kept: list[Listing] = []
    reasons: dict[str, str] = {}
    rejected: list[tuple[Listing, str]] = []
    fetched = 0
    for listings in collected.values():
        for listing in listings:
            fetched += 1
            if not is_fresh(listing, cfg.scan.max_age_hours, now_ms):
                reasons[listing.item_id] = FRESHNESS_REASON
                rejected.append((listing, reasons[listing.item_id]))
                continue
            reason = coarse_reject(listing, cfg)
            if reason:
                reasons[listing.item_id] = reason
                rejected.append((listing, reason))
                continue
            kept.append(listing)
    logger(f"  粗筛：{fetched} 条 → 通过 {len(kept)} 条")
    return kept, reasons, rejected


def _finalize(
    cfg: Config,
    conn,
    scan_id: int,
    data: _RoundData,
    watches: list[Watch],
    *,
    pages: int,
    elapsed: float,
    label: str = "",
    logger: Callable[[str], None],
) -> RoundOutcome:
    now_ms = _now_ms()
    watch_by_name = {watch.name: watch for watch in watches}
    table_rows = load_table(cfg.price_table.path) if cfg.pricing.enabled and cfg.price_table else []
    if cfg.pricing.enabled and not table_rows:
        logger("  ! 已开启价格筛选，但行情表为空：先运行 import-prices")

    new_ids: set[str] = set()
    pricing_reasons: dict[str, str] = {}
    for listing in data.coarse_kept:
        reasons = ["粗筛通过（搜索页商家判断）"]
        flags: list[str] = []
        if cfg.pricing.enabled:
            watch = watch_by_name.get(listing.watch, watches[0])
            analysis = analyze(listing, watch, table_rows, cfg, now_ms)
            if analysis.excluded:
                pricing_reasons[listing.item_id] = (analysis.flags or ["价格/品类排除"])[0]
                data.rejected.append((listing, pricing_reasons[listing.item_id]))
                continue
            if analysis.score < cfg.scan.min_score and not analysis.suspicious:
                pricing_reasons[listing.item_id] = (
                    f"评分 {analysis.score:.0f} 低于 {cfg.scan.min_score:.0f}"
                )
                data.rejected.append((listing, pricing_reasons[listing.item_id]))
                continue
            if analysis.reasons:
                reasons = analysis.reasons + reasons
            flags = analysis.flags + flags
        else:
            analysis = _neutral_analysis(reasons, flags)
        if upsert(conn, listing, analysis, scan_id):
            new_ids.add(listing.item_id)

    excluded_reasons = {**data.coarse_reasons, **pricing_reasons}
    auto_ignore_reasons = {
        item_id: reason
        for item_id, reason in excluded_reasons.items()
        if reason != FRESHNESS_REASON
    }
    auto_ignored = ignore_pending(conn, auto_ignore_reasons)
    commit(conn)
    if auto_ignored:
        logger(f"  {auto_ignored} 条旧待审核按新规则移入已忽略")

    kept_ids = {listing.item_id for listing in data.coarse_kept}
    rejected_by_id: dict[str, tuple[Listing, str]] = {}
    for listing, reason in data.rejected:
        if listing.item_id in kept_ids or listing.item_id in rejected_by_id:
            continue
        rejected_by_id[listing.item_id] = (listing, reason)
    rejected = [
        rejected_item(listing, reason, now_ms)
        for listing, reason in sorted(
            rejected_by_id.values(), key=lambda pair: float(pair[0].price or 0)
        )
    ]
    save_rejects(conn, scan_id, rejected)

    rows = rows_pending(conn, 1000)
    items = [row_to_item(row, now_ms) for row in rows]
    for item in items:
        item["is_new"] = item["item_id"] in new_ids
        # D2 卖家画像标注（只标注不排除）：阈值来自 scan.seller_review_hint
        item["pro_seller"] = is_pro_seller(item, cfg.scan.seller_review_hint)
    # 新增在前；同组里 first_seen 越新越靠前（两段稳定排序）。
    items.sort(key=lambda item: str(item.get("first_seen") or ""), reverse=True)
    items.sort(key=lambda item: 0 if item.get("is_new") else 1)

    new_count = len(new_ids)
    sort_text = _sort_text(cfg, data)
    stats = {
        "fetched": sum(len(group) for group in data.collected.values()),
        "coarse": len(data.coarse_kept),
        "rejected": len(rejected),
        "kept": len(items),
        "new": new_count,
        "watches": len(watches),
        "elapsed": elapsed,
        "top_n": cfg.scan.top_n_markdown,
        "pricing": cfg.pricing.enabled,
        "sort_status": sort_text,
    }
    title = "闲鱼游戏本 · 方案二 登录低频 · 只搜搜索页" + (f" · {label}" if label else "")
    subtitle = (
        f"搜索 {stats['fetched']} 条 · 粗筛通过 {stats['coarse']} 条 · "
        f"不符合 {stats['rejected']} 条 · 待审核 {len(items)} 条"
    )
    if data.stopped:
        paths: dict[str, Path] = {}
        logger("  · 已按 stop 结束：本轮不生成新报表")
    else:
        paths = write_all(
            cfg.reports_dir,
            items,
            title=title,
            subtitle=subtitle,
            stats=stats,
            rejected=rejected,
            review_port=cfg.review.port,
        )
    finish_scan(
        conn,
        scan_id,
        pages=pages,
        fetched=stats["fetched"],
        kept=len(items),
        error="; ".join(f"{name}：{message}" for name, message in data.errors.items()),
        sort_status=sort_text,
    )
    logger(
        f"  粗筛不符合：{len(rejected)} 条；待审核：{len(items)} 条（本轮新增 {new_count}）"
    )
    return RoundOutcome(
        scan_id=scan_id,
        items=items,
        stats=stats,
        errors=[f"{name}：{message}" for name, message in data.errors.items()],
        started="",
        elapsed=elapsed,
        paths=paths,
        stopped=data.stopped,
        sort_status=sort_text,
    )


def _search_all(
    cfg: Config,
    crawler: Crawler,
    watches: list[Watch],
    data: _RoundData,
    pages: int | None,
    stop_event: threading.Event | None,
    logger: Callable[[str], None],
    conn=None,
) -> None:
    known_ids = seen_ids(conn) if conn is not None and cfg.scan.stop_on_known else set()
    for index, watch in enumerate(watches, start=1):
        if stop_event is not None and stop_event.is_set():
            data.stopped = True
            break
        logger(f"[{index}/{len(watches)}] {watch.name}")
        try:
            collected = crawler.search(
                watch,
                pages=pages,
                stop_event=stop_event,
                known_ids=known_ids,
            )
            data.collected[watch.name] = collected
            logger(f"  收到 {len(collected)} 条")
            # 翻页失败不丢已抓页：记软失败、停后续关键词，数据照常进粗筛。
            partial = getattr(crawler, "last_search_error", "")
            if partial:
                data.errors[watch.name] = partial
                logger(f"  ! {partial}")
                break
        except LoginRequired as exc:
            data.errors[watch.name] = f"需要登录：{exc}"
            data.aborted = True
            logger(f"  ✗ {exc}")
            logger("  请先运行 login.bat 扫码登录，再重新扫描")
            break
        except RateLimited as exc:
            data.errors[watch.name] = f"频率限制：{exc}"
            data.aborted = True
            logger(f"  ✗ {exc}")
            logger("  只是被限流：等冷却过了再跑，不用重新登录，也不要连续重试")
            break
        except RiskControl as exc:
            data.errors[watch.name] = f"风控：{exc}"
            data.aborted = True
            logger(f"  ✗ {exc}")
            logger("  首次风控即停止本轮：不要连续重试")
            break
        except (CaptureTimeout, CrawlError) as exc:
            data.errors[watch.name] = str(exc)
            data.aborted = True
            logger(f"  ✗ {exc}")
            logger("  搜索阶段失败：本轮结束")
            break
        finally:
            status = getattr(crawler, "sort_status", {}).get(watch.keyword)
            if status:
                data.sort_status[watch.name] = status
        if index < len(watches) and not (stop_event and stop_event.is_set()):
            time.sleep(random.uniform(cfg.browser.min_delay, cfg.browser.max_delay))


def _collect_demo(
    cfg: Config,
    watches: list[Watch],
    logger: Callable[[str], None],
    demo_file: Path | None = None,
) -> dict[str, list[Listing]]:
    if demo_file is not None:
        from .demo import load_saved_listings

        logger(f"演示模式：回放真实抓包 {demo_file}")
        return load_saved_listings(demo_file, watches)
    from .demo import demo_listings

    return demo_listings(cfg, watches)


def _run_live_body(
    cfg: Config,
    crawler: Crawler,
    data: _RoundData,
    watches: list[Watch],
    pages: int | None,
    stop_event: threading.Event | None,
    conn,
    logger: Callable[[str], None],
) -> dict[str, int]:
    """真实抓取主体：搜索 → 粗筛（crawler 由调用方管理生命周期）。

    返回本轮请求增量；loop 跨轮复用一个浏览器时，增量才是本轮的真实用量。
    """
    before = {key: int(value) for key, value in crawler.stats.items()}
    try:
        _search_all(cfg, crawler, watches, data, pages, stop_event, logger, conn=conn)
        data.stopped = bool(stop_event and stop_event.is_set())
        data.coarse_kept, data.coarse_reasons, data.rejected = _coarse_filter(
            cfg, data.collected, _now_ms(), logger
        )
    except Exception as exc:  # noqa: BLE001 - 抓取阶段失败也要带着已有数据出报表
        data.errors["抓取"] = f"{type(exc).__name__}: {exc}"
        logger(f"  ✗ 抓取阶段失败：{type(exc).__name__}: {exc}")
    finally:
        after = {key: int(value) for key, value in crawler.stats.items()}
        delta = {
            key: after.get(key, 0) - before.get(key, 0)
            for key in set(before) | set(after)
        }
    return delta


def run_round(
    cfg: Config,
    watches: list[Watch] | None = None,
    *,
    headless: bool | None = None,
    demo: bool = False,
    demo_file: Path | None = None,
    pages: int | None = None,
    db_path: Path | None = None,
    force: bool = False,
    stop_event: threading.Event | None = None,
    label: str = "",
    dump: bool = False,
    logger: Callable[[str], None] = print,
    crawler: Crawler | None = None,
) -> RoundOutcome:
    """单轮扫描入口。

    crawler 传入时复用调用方的浏览器（loop 控制台用），本轮结束不关闭它；
    不传则本轮自行启动和关闭。
    """
    watches = watches or cfg.watches
    demo = demo or demo_file is not None
    frozen_age: float | None = None
    if demo and cfg.scan.max_age_hours:
        # 回放是历史快照：冻结时间窗，避免同一份抓包随日历推移改变计数
        frozen_age = cfg.scan.max_age_hours
        cfg = replace(cfg, scan=replace(cfg.scan, max_age_hours=0))
    cfg.ensure_dirs()
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    begin = time.monotonic()
    data = _RoundData()
    requests = {"search_requests": 0}
    groups: dict[str, list[str]] = {
        "search_risk": [],
        "fine_risk": [],
        "rate_limit": [],
        "soft": [],
    }
    status = "failed"
    error_text = ""
    conn = None
    outcome: RoundOutcome | None = None

    try:
        with ScanLock(cfg.data_dir / "scan.lock"):
            guard = RiskGuard(cfg)
            # 演示/回放不发真实请求，既不读也不写真实轮次的风控冷却。
            if not demo and not force:
                skip, message = guard.should_skip()
                if skip:
                    status = "cooldown"
                    error_text = message
                    raise ScanPaused(message)

            budget = None
            if not demo:
                budget = DailyBudget(budget_path(cfg.data_dir), cfg.scan.daily_request_budget)
                if budget.exhausted():
                    raise ScanPaused(
                        f"已达每日请求预算（{budget.describe()}）；等明天或调高 "
                        "scan.daily_request_budget"
                    )

            conn = connect(db_path or cfg.db_path)
            scan_id = start_scan(conn, len(watches))

            # 每个搜索请求发出即计入预算：Ctrl-C / 崩溃也不会少算。
            counted = {"requests": 0}

            def count_request() -> None:
                if budget is not None:
                    budget.add(1)
                counted["requests"] += 1

            def run_live(target: Crawler) -> None:
                previous = getattr(target, "on_request", None)
                target.on_request = count_request
                try:
                    delta = _run_live_body(
                        cfg, target, data, watches, pages, stop_event, conn, logger
                    )
                finally:
                    target.on_request = previous
                for key, value in delta.items():
                    requests[key] = requests.get(key, 0) + value

            try:
                if demo:
                    data.collected = _collect_demo(cfg, watches, logger, demo_file)
                    if demo_file is None:
                        logger("演示模式：使用内置数据，不联网")
                    if frozen_age is not None:
                        logger(
                            f"演示模式：时间窗按快照口径冻结"
                            f"（scan.max_age_hours {frozen_age:g} → 0）"
                        )
                    data.coarse_kept, data.coarse_reasons, data.rejected = _coarse_filter(
                        cfg, data.collected, _now_ms(), logger
                    )
                elif crawler is not None:
                    run_live(crawler)
                else:
                    dump_dir = cfg.data_dir / "captures" if dump else None
                    with Crawler(
                        cfg, headless=headless, logger=logger, dump_dir=dump_dir
                    ) as owned:
                        run_live(owned)
            except Exception as exc:  # noqa: BLE001 - 浏览器启动失败也要出已有待审核报表
                data.errors["抓取"] = f"{type(exc).__name__}: {exc}"
                logger(f"  ✗ 抓取阶段失败：{type(exc).__name__}: {exc}")

            if budget is not None:
                # 真 Crawler 已按请求即时落盘；假 crawler / 回调漏计时再补差额。
                missing = int(requests.get("search_requests", 0)) - counted["requests"]
                if missing > 0:
                    budget.add(missing)
                logger(f"  · {budget.describe()}")

            stopped = bool((stop_event and stop_event.is_set()) or data.stopped)
            data.stopped = stopped
            clean, groups = classify_round(
                watches, data.collected, data.errors, stopped=stopped
            )
            if not demo:
                guard.observe(
                    clean=clean,
                    search_errors=groups["search_risk"],
                    fine_errors=groups["fine_risk"],
                    rate_limit_errors=groups["rate_limit"],
                    soft_errors=groups["soft"],
                    logger=logger,
                )

            elapsed = time.monotonic() - begin
            outcome = _finalize(
                cfg,
                conn,
                scan_id,
                data,
                watches,
                pages=requests.get("search_requests", 0),
                elapsed=elapsed,
                label=label,
                logger=logger,
            )
            outcome.started = started_at
            outcome.stopped = stopped
            status = "stopped" if stopped else ("clean" if clean else "partial")
            return outcome
    except ScanPaused:
        raise
    except ScanBusy as exc:
        status = "locked"
        error_text = str(exc)
        raise
    except Exception as exc:
        status = "failed"
        error_text = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        log_scan(
            cfg,
            status=status,
            elapsed=time.monotonic() - begin,
            requests=requests,
            search_risk=len(groups["search_risk"]),
            fine_risk=len(groups["fine_risk"]),
            rate_limit=len(groups["rate_limit"]),
            soft_failures=len(groups["soft"]),
            outcome=outcome,
            error=error_text or "; ".join(data.errors.values()),
        )
        if conn is not None:
            conn.close()


def run_scan(
    cfg: Config,
    watches: list[Watch] | None = None,
    **kwargs,
) -> RoundOutcome:
    """单轮扫描入口，保留给 scan 命令和旧调用。"""
    return run_round(cfg, watches, **kwargs)
