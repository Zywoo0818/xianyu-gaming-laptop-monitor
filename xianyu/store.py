"""SQLite 存储：去重、记录每次扫描、保存人工审核状态。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import Analysis, Listing

STATUS_LABELS = {
    "pending": "待审核",
    "adopted": "已采纳",
    "contacted": "已联系",
    "done": "已成交",
    "ignored": "已忽略",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    item_id       TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    price         REAL NOT NULL,
    url           TEXT NOT NULL,
    image         TEXT DEFAULT '',
    location      TEXT DEFAULT '',
    seller        TEXT DEFAULT '',
    publish_ts    INTEGER DEFAULT 0,
    wants         INTEGER DEFAULT 0,
    tags          TEXT DEFAULT '[]',
    watch         TEXT DEFAULT '',
    keyword       TEXT DEFAULT '',
    ref_price     REAL DEFAULT 0,
    market_median REAL DEFAULT 0,
    ratio         REAL DEFAULT 1,
    gpu           TEXT DEFAULT '',
    brand         TEXT DEFAULT '',
    table_name    TEXT DEFAULT '',
    ref_source    TEXT DEFAULT '',
    score         REAL DEFAULT 0,
    verdict       TEXT DEFAULT '',
    reasons       TEXT DEFAULT '[]',
    flags         TEXT DEFAULT '[]',
    suspicious    INTEGER DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',
    note          TEXT DEFAULT '',
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    last_scan_id  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_listings_scan ON listings(last_scan_id);
CREATE INDEX IF NOT EXISTS idx_listings_score ON listings(score);

CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    watches     INTEGER DEFAULT 0,
    pages       INTEGER DEFAULT 0,
    fetched     INTEGER DEFAULT 0,
    kept        INTEGER DEFAULT 0,
    error       TEXT DEFAULT ''
);
"""


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(listings)")}
    additions = {
        "gpu": "TEXT DEFAULT ''",
        "brand": "TEXT DEFAULT ''",
        "table_name": "TEXT DEFAULT ''",
        "ref_source": "TEXT DEFAULT ''",
    }
    changed = False
    for name, ddl in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {name} {ddl}")
            changed = True
    if changed:
        conn.commit()


def start_scan(conn: sqlite3.Connection, watches: int) -> int:
    cursor = conn.execute(
        "INSERT INTO scans (started_at, watches) VALUES (?, ?)", (now_text(), watches)
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_scan(
    conn: sqlite3.Connection,
    scan_id: int,
    *,
    pages: int,
    fetched: int,
    kept: int,
    error: str = "",
) -> None:
    conn.execute(
        "UPDATE scans SET finished_at=?, pages=?, fetched=?, kept=?, error=? WHERE id=?",
        (now_text(), pages, fetched, kept, error, scan_id),
    )
    conn.commit()


def upsert(
    conn: sqlite3.Connection,
    listing: Listing,
    analysis: Analysis,
    scan_id: int,
) -> bool:
    """写入或更新一条商品，返回是否为新出现的商品。"""
    existing = conn.execute(
        "SELECT item_id FROM listings WHERE item_id=?", (listing.item_id,)
    ).fetchone()
    stamp = now_text()
    payload = (
        listing.title,
        listing.price,
        listing.url,
        listing.image,
        listing.location,
        listing.seller,
        listing.publish_ts,
        listing.wants,
        json.dumps(listing.tags, ensure_ascii=False),
        listing.watch,
        listing.keyword,
        analysis.ref_price,
        analysis.market_median,
        analysis.ratio,
        analysis.gpu,
        analysis.brand,
        analysis.table_name,
        analysis.ref_source,
        analysis.score,
        analysis.verdict,
        json.dumps(analysis.reasons, ensure_ascii=False),
        json.dumps(analysis.flags, ensure_ascii=False),
        1 if analysis.suspicious else 0,
        stamp,
        stamp,
        scan_id,
        listing.item_id,
    )
    conn.execute(
        """
        INSERT INTO listings (
            title, price, url, image, location, seller, publish_ts, wants, tags,
            watch, keyword, ref_price, market_median, ratio, gpu, brand,
            table_name, ref_source, score, verdict, reasons, flags, suspicious,
            first_seen, last_seen, last_scan_id, item_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(item_id) DO UPDATE SET
            title=excluded.title,
            price=excluded.price,
            image=excluded.image,
            location=excluded.location,
            seller=excluded.seller,
            publish_ts=excluded.publish_ts,
            wants=excluded.wants,
            tags=excluded.tags,
            watch=excluded.watch,
            keyword=excluded.keyword,
            ref_price=excluded.ref_price,
            market_median=excluded.market_median,
            ratio=excluded.ratio,
            gpu=excluded.gpu,
            brand=excluded.brand,
            table_name=excluded.table_name,
            ref_source=excluded.ref_source,
            score=excluded.score,
            verdict=excluded.verdict,
            reasons=excluded.reasons,
            flags=excluded.flags,
            suspicious=excluded.suspicious,
            last_seen=excluded.last_seen,
            last_scan_id=excluded.last_scan_id
        """,
        payload,
    )
    return existing is None


def commit(conn: sqlite3.Connection) -> None:
    conn.commit()


def rows_for_scan(conn: sqlite3.Connection, scan_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM listings
            WHERE last_scan_id=? AND status<>'ignored'
            ORDER BY suspicious ASC, score DESC, price ASC
            """,
            (scan_id,),
        )
    )


def rows_ignored(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM listings WHERE status='ignored' ORDER BY last_seen DESC LIMIT ?",
            (limit,),
        )
    )


def ignored_ids(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["item_id"])
        for row in conn.execute("SELECT item_id FROM listings WHERE status='ignored'")
    }


def ignore_pending(conn: sqlite3.Connection, reasons: dict[str, str]) -> int:
    """只自动忽略仍在待审核的旧条目，不覆盖人工处理过的状态。"""
    changed = 0
    for item_id, reason in reasons.items():
        cursor = conn.execute(
            "UPDATE listings SET status='ignored', note=? WHERE item_id=? AND status='pending'",
            (f"自动忽略：{reason}", item_id),
        )
        changed += cursor.rowcount
    if changed:
        conn.commit()
    return changed


def rows_pending(conn: sqlite3.Connection, limit: int = 300) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM listings
            WHERE status='pending' AND suspicious=0
            ORDER BY score DESC, price ASC LIMIT ?
            """,
            (limit,),
        )
    )


def rows_suspicious(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM listings
            WHERE status='pending' AND suspicious=1
            ORDER BY score DESC LIMIT ?
            """,
            (limit,),
        )
    )


def list_rows_by_status(conn: sqlite3.Connection, status: str | None, limit: int = 30) -> list[sqlite3.Row]:
    if status:
        return list(
            conn.execute(
                "SELECT * FROM listings WHERE status=? ORDER BY suspicious ASC, score DESC LIMIT ?",
                (status, limit),
            )
        )
    return list(
        conn.execute(
            "SELECT * FROM listings ORDER BY suspicious ASC, score DESC LIMIT ?", (limit,)
        )
    )


def latest_scan(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()


def get_scan(conn: sqlite3.Connection, scan_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()


def set_status(conn: sqlite3.Connection, item_id: str, status: str, note: str | None = None) -> bool:
    if status not in STATUS_LABELS:
        raise ValueError(f"未知状态：{status}")
    if note is None:
        cursor = conn.execute(
            "UPDATE listings SET status=? WHERE item_id=?", (status, item_id)
        )
    else:
        cursor = conn.execute(
            "UPDATE listings SET status=?, note=? WHERE item_id=?",
            (status, note, item_id),
        )
    conn.commit()
    return cursor.rowcount > 0


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM listings GROUP BY status")
    return {row["status"]: row["n"] for row in rows}
