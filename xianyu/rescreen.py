"""D3 积压重筛：把现行粗筛规则套到库里所有待审核商品上。

规则变更（v3.6 显卡门槛、v3.7 卖家词、v3.8.0 24 小时时间窗）会留下一批
按旧规则入库的待审核。本模块用**同一套**现行规则（`is_fresh` +
`coarse_reject`，与引擎搜索路径完全一致，不新写规则）重新过一遍
`status='pending'` 的商品，把不再符合的移入「不符合」：

- `plan_rescreen()`：纯只读规划，返回（保留, 移出+原因），可离线测试；
- `apply_rescreen()`：建一轮专用的「人工重筛」扫描记录（不发任何请求），
  把移出商品写进该轮 `scan_rejects`（原因 = 规则原因），并置
  `status='ignored'、note='重筛排除：<原因>'`——与「人工审核」前缀一样
  不进「自动忽略」标签，避免和不符合板块重复展示；
- 通过的商品一律不动；没有可移商品时不再建新轮次（重复执行幂等）。

命令行：`python -m xianyu rescreen`（默认 dry-run）/ `--write` 落库。
"""
from __future__ import annotations

import sqlite3
import time

from .coarse import coarse_reject
from .config import Config
from .engine import FRESHNESS_REASON
from .models import Listing
from .pricing import is_fresh
from .store import RESCREEN_NOTE_PREFIX, connect, rows_pending

# 专用轮次的 sort_status 标记：一眼看出这不是真实抓取轮
RESCREEN_SORT_STATUS = "人工重筛：积压清理（未发请求）"


def _listing_from_row(row: sqlite3.Row) -> Listing:
    """把库里的待审核行还原成粗筛可用的 Listing。

    搜索接口的 title 本来就是「标题 + 描述全文」，desc 留空不影响规则。
    """
    return Listing(
        item_id=row["item_id"],
        title=row["title"] or "",
        price=float(row["price"] or 0),
        url=row["url"] or "",
        image=row["image"] or "",
        location=row["location"] or "",
        seller=row["seller"] or "",
        seller_id=row["seller_id"] or "",
        publish_ts=int(row["publish_ts"] or 0),
        keyword=row["keyword"] or "",
        watch=row["watch"] or "",
    )


def rescreen_reason(listing: Listing, cfg: Config, now_ms: int) -> str:
    """返回排除原因；空字符串 = 按现行规则通过。"""
    if not is_fresh(listing, cfg.scan.max_age_hours, now_ms):
        return FRESHNESS_REASON
    return coarse_reject(listing, cfg)


def plan_rescreen(
    conn: sqlite3.Connection, cfg: Config, now_ms: int
) -> tuple[list[sqlite3.Row], list[tuple[sqlite3.Row, str]]]:
    """只读规划：返回（保留的行, [(移出的行, 原因)]）。"""
    keep: list[sqlite3.Row] = []
    move: list[tuple[sqlite3.Row, str]] = []
    for row in rows_pending(conn, 100000):
        reason = rescreen_reason(_listing_from_row(row), cfg, now_ms)
        if reason:
            move.append((row, reason))
        else:
            keep.append(row)
    return keep, move


def apply_rescreen(
    conn: sqlite3.Connection,
    cfg: Config,
    now_ms: int,
    logger=lambda _message: None,
) -> dict:
    """落库：建专用轮次，把移出商品写进该轮不符合明细并置 ignored。

    返回统计 dict；没有可移商品时不建轮次（幂等）。
    """
    keep, move = plan_rescreen(conn, cfg, now_ms)
    if not move:
        logger("  没有需要移出的商品，未创建新轮次")
        return {
            "scan_id": None,
            "scanned": len(keep),
            "kept": len(keep),
            "moved": 0,
            "by_reason": {},
        }

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute(
        """
        INSERT INTO scans
            (started_at, finished_at, watches, pages, fetched, kept, error, sort_status)
        VALUES (?, ?, 0, 0, ?, ?, '', ?)
        """,
        (stamp, stamp, len(keep) + len(move), len(keep), RESCREEN_SORT_STATUS),
    )
    scan_id = int(cursor.lastrowid)

    by_reason: dict[str, int] = {}
    for row, reason in move:
        conn.execute(
            """
            INSERT OR REPLACE INTO scan_rejects (
                scan_id, item_id, title, price, url, image, location, seller,
                seller_id, publish_ts, watch, keyword, reason
            )
            SELECT ?, item_id, title, price, url, image, location, seller,
                   seller_id, publish_ts, watch, keyword, ?
            FROM listings WHERE item_id=?
            """,
            (scan_id, reason, row["item_id"]),
        )
        conn.execute(
            "UPDATE listings SET status='ignored', note=? WHERE item_id=? AND status='pending'",
            (f"{RESCREEN_NOTE_PREFIX}{reason}", row["item_id"]),
        )
        by_reason[reason] = by_reason.get(reason, 0) + 1
    conn.commit()

    logger(f"  重筛轮次 #{scan_id}：待审核 {len(keep) + len(move)} 条 → 保留 {len(keep)} / 移出 {len(move)}")
    for reason, count in sorted(by_reason.items(), key=lambda pair: -pair[1]):
        logger(f"    {count} 条 {reason}")
    return {
        "scan_id": scan_id,
        "scanned": len(keep) + len(move),
        "kept": len(keep),
        "moved": len(move),
        "by_reason": by_reason,
    }
