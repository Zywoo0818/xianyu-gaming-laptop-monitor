"""扫描编排：抓取 → 过滤/比价/打分 → 去重入库 → 出报表。"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import Config, Watch
from .crawler import CaptureTimeout, Crawler, CrawlError, LoginRequired, RiskControl
from .models import Analysis, Listing
from .pricetable import PriceRow, load_table
from .pricing import analyze, is_fresh, passes_filters
from .report import row_to_item, write_all
from .store import (
    commit,
    connect,
    finish_scan,
    ignore_pending,
    ignored_ids,
    rows_for_scan,
    start_scan,
    upsert,
)


@dataclass
class ScanOutcome:
    scan_id: int
    items: list[dict]
    stats: dict
    errors: list[str] = field(default_factory=list)
    started: str = ""
    elapsed: float = 0.0
    paths: dict[str, Path] = field(default_factory=dict)

    @property
    def new_count(self) -> int:
        return sum(1 for item in self.items if item.get("is_new"))

    @property
    def recommended(self) -> list[dict]:
        return [item for item in self.items if not item["suspicious"]]


def _now_ms() -> int:
    return int(time.time() * 1000)


def shortlist(
    cfg: Config,
    collected: dict[str, list[Listing]],
    table_rows: list[PriceRow],
    ignored: set[str],
    now_ms: int,
) -> list[Listing]:
    """挑出值得抓详情页的候选，条件和 evaluate 的保留条件保持一致。"""
    limit = cfg.scan.detail_limit
    if limit <= 0:
        return []
    best: dict[str, tuple[float, Listing]] = {}
    for watch in cfg.watches:
        for listing in collected.get(watch.name, []):
            if listing.item_id in ignored:
                continue
            if not is_fresh(listing, cfg.scan.max_age_hours, now_ms):
                continue
            if not passes_filters(listing, watch, cfg):
                continue
            analysis = analyze(listing, watch, table_rows, cfg, now_ms)
            if analysis.excluded:
                continue
            if analysis.score < cfg.scan.min_score and not analysis.suspicious:
                continue
            previous = best.get(listing.item_id)
            if previous is None or analysis.score > previous[0]:
                best[listing.item_id] = (analysis.score, listing)
    ordered = sorted(best.values(), key=lambda pair: -pair[0])
    return [listing for _, listing in ordered[:limit]]


def _enrich_details(
    cfg: Config,
    crawler: Crawler,
    collected: dict[str, list[Listing]],
    *,
    db_path: Path | None,
    logger=print,
) -> None:
    if cfg.scan.detail_limit <= 0:
        return
    conn = connect(db_path or cfg.db_path)
    try:
        ignored = ignored_ids(conn)
    finally:
        conn.close()
    table_rows = load_table(cfg.price_table.path) if cfg.price_table else []
    candidates = shortlist(cfg, collected, table_rows, ignored, _now_ms())
    if not candidates:
        logger("  细筛：没有需要抓详情页的候选")
        return
    logger(f"  细筛：抓取 {len(candidates)} 条候选详情（上限 {cfg.scan.detail_limit} 条）")
    details = crawler.fetch_details(candidates)
    if not details:
        return
    for listings in collected.values():
        for listing in listings:
            desc = details.get(listing.item_id)
            if desc:
                listing.desc = desc
    logger(f"  细筛：拿到 {len(details)}/{len(candidates)} 条详情")


def collect_live(
    cfg: Config,
    watches: list[Watch],
    *,
    headless: bool | None = None,
    pages: int | None = None,
    on_collected: Callable[[Crawler, dict[str, list[Listing]]], None] | None = None,
    logger=print,
) -> tuple[dict[str, list[Listing]], dict[str, str]]:
    results: dict[str, list[Listing]] = {}
    errors: dict[str, str] = {}
    with Crawler(cfg, headless=headless, logger=logger) as crawler:
        for index, watch in enumerate(watches, start=1):
            logger(f"[{index}/{len(watches)}] {watch.name}")
            try:
                results[watch.name] = crawler.search(watch, pages=pages)
                logger(f"  收到 {len(results[watch.name])} 条")
            except LoginRequired as exc:
                errors[watch.name] = f"需要登录：{exc}"
                logger(f"  ✗ {exc}")
                break
            except RiskControl as exc:
                errors[watch.name] = f"风控：{exc}"
                logger(f"  ✗ {exc}")
                time.sleep(5)
            except (CaptureTimeout, CrawlError) as exc:
                errors[watch.name] = str(exc)
                logger(f"  ✗ {exc}")
            if index < len(watches):
                time.sleep(random.uniform(cfg.browser.min_delay, cfg.browser.max_delay))
        if (
            on_collected is not None
            and any(results.values())
            and not any("需要登录" in message for message in errors.values())
        ):
            try:
                on_collected(crawler, results)
            except LoginRequired as exc:
                errors["详情"] = f"详情需要登录：{exc}"
                logger(f"  ✗ 详情阶段中止：{exc}")
            except RiskControl as exc:
                errors["详情"] = f"详情风控：{exc}"
                logger(f"  ✗ 详情阶段中止：{exc}")
            except (CaptureTimeout, CrawlError) as exc:
                errors["详情"] = f"详情抓取失败：{exc}"
                logger(f"  ✗ {exc}")
            except Exception as exc:  # noqa: BLE001 - 详情失败不能拖垮已拿到的搜索结果
                errors["详情"] = f"详情抓取失败：{exc}"
                logger(f"  ✗ 详情阶段失败：{str(exc)[:120]}")
    return results, errors


def collect_demo(cfg: Config, logger=print) -> tuple[dict[str, list[Listing]], dict[str, str]]:
    from .demo import demo_listings

    logger("演示模式：使用内置模拟数据，不联网、不需要登录。")
    return demo_listings(cfg), {}


def evaluate(
    cfg: Config,
    collected: dict[str, list[Listing]],
    errors: dict[str, str],
    *,
    db_path: Path | None = None,
    logger=print,
    label: str = "",
) -> ScanOutcome:
    cfg.ensure_dirs()
    conn = connect(db_path or cfg.db_path)
    watch_errors = sum(1 for name in errors if name != "详情")
    scan_id = start_scan(conn, len(collected) + watch_errors)
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    begin = time.monotonic()
    now_ms = _now_ms()
    table_rows = load_table(cfg.price_table.path) if cfg.price_table else []
    if not table_rows:
        logger("  ! 行情表为空：先运行 python -m xianyu import-prices 导入你的 xlsx")

    best: dict[str, tuple[Listing, Analysis]] = {}
    fetched_total = 0
    gated_total = 0
    error_messages = [f"{name}：{message}" for name, message in errors.items()]
    gate_reasons: dict[str, int] = {}
    fatal_reasons: dict[str, str] = {}

    for watch in cfg.watches:
        listings = collected.get(watch.name)
        if listings is None:
            continue
        kept = [
            listing
            for listing in listings
            if is_fresh(listing, cfg.scan.max_age_hours, now_ms) and passes_filters(listing, watch, cfg)
        ]
        fetched_total += len(listings)
        logger(f"  {watch.name}：{len(listings)} 条 → 价格/词表过滤后 {len(kept)} 条")

        for listing in kept:
            analysis = analyze(listing, watch, table_rows, cfg, now_ms)
            if analysis.excluded:
                reason = (analysis.flags or ["排除"])[0]
                gate_reasons[reason] = gate_reasons.get(reason, 0) + 1
                if analysis.fatal:
                    fatal_reasons[listing.item_id] = reason
                continue
            gated_total += 1
            if analysis.score < cfg.scan.min_score and not analysis.suspicious:
                continue
            previous = best.get(listing.item_id)
            if previous is None or analysis.score > previous[1].score:
                best[listing.item_id] = (listing, analysis)

    new_ids: set[str] = set()
    for item_id, (listing, analysis) in best.items():
        if upsert(conn, listing, analysis, scan_id):
            new_ids.add(item_id)
    auto_ignored = ignore_pending(conn, fatal_reasons)
    commit(conn)
    if auto_ignored:
        logger(f"  {auto_ignored} 条旧商品按硬规则移入已忽略")

    rows = rows_for_scan(conn, scan_id)
    kept_total = len(rows)
    finish_scan(
        conn,
        scan_id,
        pages=0,
        fetched=fetched_total,
        kept=kept_total,
        error="; ".join(error_messages),
    )

    elapsed = time.monotonic() - begin
    stats = {
        "fetched": fetched_total,
        "kept": kept_total,
        "gated": gated_total,
        "watches": len(collected),
        "elapsed": elapsed,
        "top_n": cfg.scan.top_n_markdown,
    }
    items = [row_to_item(row, now_ms) for row in rows]
    for item in items:
        item["is_new"] = item["item_id"] in new_ids
    items.sort(key=lambda item: (item["suspicious"], -item["score"]))

    if gate_reasons:
        top = sorted(gate_reasons.items(), key=lambda pair: -pair[1])[:4]
        logger("  排除原因：" + "；".join(f"{reason} {count}" for reason, count in top))

    title = "闲鱼游戏本 · 低价筛选" + (f" · {label}" if label else "")
    subtitle = f"共 {len(collected)} 个关键词 · 抓到 {fetched_total} 条 · 入选 {len(items)} 条"
    paths = write_all(
        cfg.reports_dir,
        items,
        title=title,
        subtitle=subtitle,
        stats=stats,
    )
    conn.close()
    return ScanOutcome(
        scan_id=scan_id,
        items=items,
        stats=stats,
        errors=error_messages,
        started=started_at,
        elapsed=elapsed,
        paths=paths,
    )


def run_scan(
    cfg: Config,
    watches: list[Watch] | None = None,
    *,
    headless: bool | None = None,
    demo: bool = False,
    pages: int | None = None,
    db_path: Path | None = None,
    logger=print,
) -> ScanOutcome:
    watches = watches or cfg.watches
    if demo:
        collected, errors = collect_demo(cfg, logger=logger)
        return evaluate(cfg, collected, errors, db_path=db_path, logger=logger, label="演示数据")

    def on_collected(crawler: Crawler, collected: dict[str, list[Listing]]) -> None:
        _enrich_details(cfg, crawler, collected, db_path=db_path, logger=logger)

    collected, errors = collect_live(
        cfg,
        watches,
        headless=headless,
        pages=pages,
        on_collected=on_collected,
        logger=logger,
    )
    fatal = any("需要登录" in message for message in errors.values())
    risk_blocked = bool(errors) and not any(collected.values())
    if risk_blocked and not fatal and headless is not False and cfg.browser.headful_fallback:
        logger("无头模式没拿到数据，改用有头模式重试一次…")
        collected, errors = collect_live(
            cfg,
            watches,
            headless=False,
            pages=pages,
            on_collected=on_collected,
            logger=logger,
        )

    return evaluate(cfg, collected, errors, db_path=db_path, logger=logger)
