"""扫描编排：抓取 → 卖家审核 → 过滤/比价/打分 → 去重入库 → 出报表。"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Config, Watch
from .crawler import CaptureTimeout, Crawler, CrawlError, LoginRequired, RiskControl
from .models import Analysis, Listing
from .pricetable import load_table
from .pricing import analyze, is_fresh, passes_filters
from .report import row_to_item, write_all
from .store import (
    commit,
    connect,
    finish_scan,
    rows_for_scan,
    save_median,
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


def collect_live(
    cfg: Config,
    watches: list[Watch],
    *,
    headless: bool | None = None,
    pages: int | None = None,
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
    audits: dict[str, dict] | None = None,
    db_path: Path | None = None,
    logger=print,
    label: str = "",
) -> ScanOutcome:
    cfg.ensure_dirs()
    conn = connect(db_path or cfg.db_path)
    scan_id = start_scan(conn, len(collected) + len(errors))
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    begin = time.monotonic()
    now_ms = _now_ms()
    table_rows = load_table(cfg.price_table.path) if cfg.price_table else []
    if not table_rows:
        logger("  ! 行情表为空：先运行 python -m xianyu import-prices 导入你的 xlsx")
    audits = audits or {}

    best: dict[str, tuple[Listing, Analysis, dict | None]] = {}
    fetched_total = 0
    gated_total = 0
    error_messages = [f"{name}：{message}" for name, message in errors.items()]
    gate_reasons: dict[str, int] = {}

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
            audit = audits.get(listing.item_id)
            analysis = analyze(listing, watch, table_rows, cfg, now_ms, audit)
            if analysis.excluded:
                reason = (analysis.flags or ["排除"])[0]
                gate_reasons[reason] = gate_reasons.get(reason, 0) + 1
                continue
            gated_total += 1
            if analysis.score < cfg.scan.min_score and not analysis.suspicious:
                continue
            previous = best.get(listing.item_id)
            if previous is None or analysis.score > previous[1].score:
                best[listing.item_id] = (listing, analysis, audit)

    new_ids: set[str] = set()
    for item_id, (listing, analysis, audit) in best.items():
        if upsert(conn, listing, analysis, scan_id, audit):
            new_ids.add(item_id)
    commit(conn)
    finish_scan(
        conn,
        scan_id,
        pages=0,
        fetched=fetched_total,
        kept=len(best),
        error="; ".join(error_messages),
    )
    rows = rows_for_scan(conn, scan_id)

    elapsed = time.monotonic() - begin
    stats = {
        "fetched": fetched_total,
        "kept": len(best),
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
    subtitle = f"共 {len(collected)} 个关键词 · 抓到 {fetched_total} 条 · 入选 {len(best)} 条"
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

    collected, errors = collect_live(cfg, watches, headless=headless, pages=pages, logger=logger)
    fatal = any("需要登录" in message for message in errors.values())
    risk_blocked = bool(errors) and not any(collected.values())
    if risk_blocked and not fatal and headless is not False and cfg.browser.headful_fallback:
        logger("无头模式没拿到数据，改用有头模式重试一次…")
        collected, errors = collect_live(cfg, watches, headless=False, pages=pages, logger=logger)

    audits: dict[str, dict] = {}
    all_listings = [listing for group in collected.values() for listing in group]
    if cfg.audit.enabled and all_listings:
        from .audit import audit_sellers

        audits = audit_sellers(cfg, all_listings, logger=logger)
    return evaluate(cfg, collected, errors, audits=audits, db_path=db_path, logger=logger)
