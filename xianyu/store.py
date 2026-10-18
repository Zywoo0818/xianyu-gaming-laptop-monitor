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
    samples       INTEGER DEFAULT 0,
    ratio         REAL DEFAULT 1,
    gpu           TEXT DEFAULT '',
    brand         TEXT DEFAULT '',
    table_name    TEXT DEFAULT '',
    ref_source    TEXT DEFAULT '',
    audit         TEXT DEFAULT '{}',
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

CREATE TABLE IF NOT EXISTS price_history (
    watch   TEXT NOT NULL,
    day     TEXT NOT NULL,
    median  REAL NOT NULL,
    samples INTEGER NOT NULL,
    PRIMARY KEY (watch, day)
);

CREATE TABLE IF NOT EXISTS seller_audit (
    nick        TEXT PRIMARY KEY,
    user_id     TEXT DEFAULT '',
    has_dji     INTEGER DEFAULT 0,
    apple_hits  INTEGER DEFAULT 0,
    merchant    INTEGER DEFAULT 0,
    in_sale     INTEGER DEFAULT 0,
    sold        INTEGER DEFAULT 0,
    bio         TEXT DEFAULT '',
    titles      TEXT DEFAULT '[]',
    checked_at  TEXT NOT NULL
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
        "samples": "INTEGER DEFAULT 0",
        "gpu": "TEXT DEFAULT ''",
        "brand": "TEXT DEFAULT ''",
        "table_name": "TEXT DEFAULT ''",
        "ref_source": "TEXT DEFAULT ''",
        "audit": "TEXT DEFAULT '{}'",
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
    audit: dict | None = None,
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
        analysis.samples,
        analysis.ratio,
        analysis.gpu,
        analysis.brand,
        analysis.table_name,
        analysis.ref_source,
        json.dumps(audit or {}, ensure_ascii=False),
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
            watch, keyword, ref_price, market_median, samples, ratio, gpu, brand,
            table_name, ref_source, audit, score, verdict, reasons, flags, suspicious,
            first_seen, last_seen, last_scan_id, item_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            samples=excluded.samples,
            ratio=excluded.ratio,
            gpu=excluded.gpu,
            brand=excluded.brand,
            table_name=excluded.table_name,
            ref_source=excluded.ref_source,
            audit=excluded.audit,
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
            "SELECT * FROM listings WHERE last_scan_id=? ORDER BY suspicious ASC, score DESC, price ASC",
            (scan_id,),
        )
    )


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


def save_median(conn: sqlite3.Connection, watch: str, median: float, samples: int) -> None:
    if median <= 0 or samples <= 0:
        return
    conn.execute(
        "INSERT OR REPLACE INTO price_history (watch, day, median, samples) VALUES (?,?,?,?)",
        (watch, datetime.now().strftime("%Y-%m-%d"), median, samples),
    )
    conn.commit()


def get_seller_audit(conn: sqlite3.Connection, nick: str, max_age_hours: float = 168) -> dict | None:
    row = conn.execute("SELECT * FROM seller_audit WHERE nick=?", (nick,)).fetchone()
    if row is None:
        return None
    try:
        age = (datetime.now() - datetime.strptime(row["checked_at"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
    except (TypeError, ValueError):
        return None
    if age > max_age_hours:
        return None
    return {
        "user_id": row["user_id"],
        "has_dji": bool(row["has_dji"]),
        "apple_hits": row["apple_hits"],
        "merchant": bool(row["merchant"]),
        "in_sale": row["in_sale"],
        "sold": row["sold"],
        "bio": row["bio"],
        "titles": json.loads(row["titles"] or "[]"),
        "cached": True,
    }


def save_seller_audit(conn: sqlite3.Connection, nick: str, data: dict) -> None:
    conn.execute(
        """
        INSERT INTO seller_audit (nick, user_id, has_dji, apple_hits, merchant, in_sale, sold, bio, titles, checked_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(nick) DO UPDATE SET
            user_id=excluded.user_id, has_dji=excluded.has_dji, apple_hits=excluded.apple_hits,
            merchant=excluded.merchant, in_sale=excluded.in_sale, sold=excluded.sold,
            bio=excluded.bio, titles=excluded.titles, checked_at=excluded.checked_at
        """,
        (
            nick,
            str(data.get("user_id") or ""),
            1 if data.get("has_dji") else 0,
            int(data.get("apple_hits") or 0),
            1 if data.get("merchant") else 0,
            int(data.get("in_sale") or 0),
            int(data.get("sold") or 0),
            str(data.get("bio") or "")[:500],
            json.dumps(data.get("titles") or [], ensure_ascii=False)[:4000],
            now_text(),
        ),
    )
    conn.commit()
