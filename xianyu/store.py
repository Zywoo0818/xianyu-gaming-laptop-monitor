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

MANUAL_REJECT_REASON = "人工审核"

# D3 积压重筛移出的商品：与「人工审核」一样只进不符合板块，不进自动忽略
RESCREEN_NOTE_PREFIX = "重筛排除："

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    item_id       TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    price         REAL NOT NULL,
    url           TEXT NOT NULL,
    image         TEXT DEFAULT '',
    location      TEXT DEFAULT '',
    seller        TEXT DEFAULT '',
    seller_id     TEXT DEFAULT '',
    publish_ts    INTEGER DEFAULT 0,
    wants         INTEGER DEFAULT 0,
    seller_reviews INTEGER DEFAULT 0,
    seller_rating  INTEGER DEFAULT 0,
    seller_identity TEXT DEFAULT '',
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
    notified_at   TEXT DEFAULT '',
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    last_scan_id  INTEGER,
    first_scan_id INTEGER
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
    error       TEXT DEFAULT '',
    sort_status TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS scan_rejects (
    scan_id    INTEGER NOT NULL,
    item_id    TEXT NOT NULL,
    title      TEXT NOT NULL,
    price      REAL NOT NULL,
    url        TEXT NOT NULL,
    image      TEXT DEFAULT '',
    location   TEXT DEFAULT '',
    seller     TEXT DEFAULT '',
    seller_id  TEXT DEFAULT '',
    publish_ts INTEGER DEFAULT 0,
    watch      TEXT DEFAULT '',
    keyword    TEXT DEFAULT '',
    reason     TEXT NOT NULL,
    PRIMARY KEY (scan_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_scan_rejects_scan ON scan_rejects(scan_id);

CREATE TABLE IF NOT EXISTS listing_details (
    item_id    TEXT PRIMARY KEY,
    desc       TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seller_checks (
    seller_id    TEXT PRIMARY KEY,
    seller_name  TEXT DEFAULT '',
    verdict      TEXT NOT NULL,
    reason       TEXT DEFAULT '',
    item_count   INTEGER DEFAULT 0,
    laptop_count INTEGER DEFAULT 0,
    part_count   INTEGER DEFAULT 0,
    in_sale      INTEGER DEFAULT 0,
    checked_ts   INTEGER NOT NULL,
    checked_at   TEXT NOT NULL
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
        "seller_id": "TEXT DEFAULT ''",
        "gpu": "TEXT DEFAULT ''",
        "brand": "TEXT DEFAULT ''",
        "table_name": "TEXT DEFAULT ''",
        "ref_source": "TEXT DEFAULT ''",
        "notified_at": "TEXT DEFAULT ''",
        "first_scan_id": "INTEGER",
        "seller_reviews": "INTEGER DEFAULT 0",
        "seller_rating": "INTEGER DEFAULT 0",
        "seller_identity": "TEXT DEFAULT ''",
    }
    changed = False
    for name, ddl in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {name} {ddl}")
            changed = True
            if name == "notified_at":
                # 老库升级时不补发历史商品：已有的都当作“已通知过”。
                conn.execute("UPDATE listings SET notified_at=last_seen")
    scan_columns = {row["name"] for row in conn.execute("PRAGMA table_info(scans)")}
    if "sort_status" not in scan_columns:
        conn.execute("ALTER TABLE scans ADD COLUMN sort_status TEXT DEFAULT ''")
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
    sort_status: str = "",
) -> None:
    conn.execute(
        "UPDATE scans SET finished_at=?, pages=?, fetched=?, kept=?, error=?, "
        "sort_status=? WHERE id=?",
        (now_text(), pages, fetched, kept, error, sort_status, scan_id),
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
        listing.seller_id,
        listing.publish_ts,
        listing.wants,
        listing.seller_reviews,
        listing.seller_rating,
        listing.seller_identity,
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
        scan_id,
        listing.item_id,
    )
    conn.execute(
        """
        INSERT INTO listings (
            title, price, url, image, location, seller, seller_id, publish_ts, wants,
            seller_reviews, seller_rating, seller_identity, tags,
            watch, keyword, ref_price, market_median, ratio, gpu, brand,
            table_name, ref_source, score, verdict, reasons, flags, suspicious,
            first_seen, last_seen, last_scan_id, first_scan_id, item_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(item_id) DO UPDATE SET
            title=excluded.title,
            price=excluded.price,
            url=excluded.url,
            image=excluded.image,
            location=excluded.location,
            seller=excluded.seller,
            seller_id=excluded.seller_id,
            publish_ts=excluded.publish_ts,
            wants=excluded.wants,
            seller_reviews=excluded.seller_reviews,
            seller_rating=excluded.seller_rating,
            seller_identity=excluded.seller_identity,
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


def save_rejects(conn: sqlite3.Connection, scan_id: int, rows: list[dict]) -> int:
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT OR REPLACE INTO scan_rejects (
            scan_id, item_id, title, price, url, image, location, seller,
            seller_id, publish_ts, watch, keyword, reason
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                scan_id,
                row["item_id"],
                row["title"],
                float(row.get("price") or 0),
                row["url"],
                row.get("image", ""),
                row.get("location", ""),
                row.get("seller", ""),
                row.get("seller_id", ""),
                int(row.get("publish_ts") or 0),
                row.get("watch", ""),
                row.get("keyword", ""),
                row.get("reason", ""),
            )
            for row in rows
        ],
    )
    conn.commit()
    return len(rows)


def rows_rejects(conn: sqlite3.Connection, scan_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM scan_rejects WHERE scan_id=? ORDER BY price ASC, item_id",
            (scan_id,),
        )
    )


def rows_ignored(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    """自动忽略的商品；人工忽略和积压重筛移出的进不符合板块，不在这里重复展示。"""
    return list(
        conn.execute(
            """
            SELECT * FROM listings
            WHERE status='ignored'
              AND COALESCE(note, '') NOT LIKE ?
              AND COALESCE(note, '') NOT LIKE ?
            ORDER BY last_seen DESC LIMIT ?
            """,
            (f"{MANUAL_REJECT_REASON}%", f"{RESCREEN_NOTE_PREFIX}%", limit),
        )
    )


def rows_manual_rejects(
    conn: sqlite3.Connection, scan_id: int, limit: int = 500
) -> list[sqlite3.Row]:
    """人工忽略 / 积压重筛移出的商品：跨轮保留。

    「不符合」板块默认是最新一轮的 `scan_rejects`。人工忽略和重筛的记录写在
    它们当时那一轮，一旦跑了新扫描就会被冲掉——这里把还没出现在本轮明细里的
    人工/重筛记录补回来，避免商品进了无人区（待审核、不符合、自动忽略都没有）。
    """
    return list(
        conn.execute(
            """
            SELECT * FROM listings
            WHERE status='ignored'
              AND (COALESCE(note, '') LIKE ? OR COALESCE(note, '') LIKE ?)
              AND item_id NOT IN (SELECT item_id FROM scan_rejects WHERE scan_id=?)
            ORDER BY last_seen DESC LIMIT ?
            """,
            (
                f"{MANUAL_REJECT_REASON}%",
                f"{RESCREEN_NOTE_PREFIX}%",
                scan_id,
                limit,
            ),
        )
    )


def ignored_ids(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["item_id"])
        for row in conn.execute("SELECT item_id FROM listings WHERE status='ignored'")
    }


def seen_ids(conn: sqlite3.Connection) -> set[str]:
    """库里所有出现过的商品 ID：翻页时判断「整页都是老货」用。"""
    return {str(row["item_id"]) for row in conn.execute("SELECT item_id FROM listings")}


def load_detail_cache(conn: sqlite3.Connection) -> tuple[dict[str, str], set[str]]:
    """返回 (item_id -> 已缓存描述, 不需要再抓详情的 item_id)。"""
    details = {
        str(row["item_id"]): str(row["desc"] or "")
        for row in conn.execute("SELECT item_id, desc FROM listing_details")
    }
    skip_ids = set(details)
    for row in conn.execute("SELECT item_id FROM listings WHERE status<>'pending'"):
        skip_ids.add(str(row["item_id"]))
    return details, skip_ids


def save_details(conn: sqlite3.Connection, details: dict[str, str]) -> int:
    if not details:
        return 0
    stamp = now_text()
    conn.executemany(
        """
        INSERT INTO listing_details (item_id, desc, fetched_at) VALUES (?,?,?)
        ON CONFLICT(item_id) DO UPDATE SET desc=excluded.desc, fetched_at=excluded.fetched_at
        """,
        [(item_id, desc, stamp) for item_id, desc in details.items()],
    )
    conn.commit()
    return len(details)


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


def rows_pending(conn: sqlite3.Connection, limit: int = 400) -> list[sqlite3.Row]:
    """所有未审核商品；跨轮保留，按第一次出现时间倒序。"""
    return list(
        conn.execute(
            """
            SELECT * FROM listings
            WHERE status='pending'
            ORDER BY first_seen DESC, price ASC LIMIT ?
            """,
            (limit,),
        )
    )


def load_seller_checks(
    conn: sqlite3.Connection, seller_ids: set[str] | None = None
) -> dict[str, sqlite3.Row]:
    if seller_ids is None:
        rows = conn.execute("SELECT * FROM seller_checks")
    else:
        if not seller_ids:
            return {}
        marks = ",".join("?" for _ in seller_ids)
        rows = conn.execute(
            f"SELECT * FROM seller_checks WHERE seller_id IN ({marks})",
            tuple(seller_ids),
        )
    return {str(row["seller_id"]): row for row in rows}


def save_seller_checks(conn: sqlite3.Connection, checks: list[dict]) -> int:
    if not checks:
        return 0
    conn.executemany(
        """
        INSERT INTO seller_checks (
            seller_id, seller_name, verdict, reason, item_count,
            laptop_count, part_count, in_sale, checked_ts, checked_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(seller_id) DO UPDATE SET
            seller_name=excluded.seller_name,
            verdict=excluded.verdict,
            reason=excluded.reason,
            item_count=excluded.item_count,
            laptop_count=excluded.laptop_count,
            part_count=excluded.part_count,
            in_sale=excluded.in_sale,
            checked_ts=excluded.checked_ts,
            checked_at=excluded.checked_at
        """,
        [
            (
                check["seller_id"],
                check.get("seller_name", ""),
                check["verdict"],
                check.get("reason", ""),
                int(check.get("item_count", 0)),
                int(check.get("laptop_count", 0)),
                int(check.get("part_count", 0)),
                int(check.get("in_sale", 0)),
                int(check["checked_ts"]),
                check["checked_at"],
            )
            for check in checks
        ],
    )
    conn.commit()
    return len(checks)


def pending_notify(conn: sqlite3.Connection, min_score: float, limit: int = 50) -> list[sqlite3.Row]:
    """待推送队列：分数够、未被忽略、还没成功推送过的商品。"""
    return list(
        conn.execute(
            """
            SELECT * FROM listings
            WHERE (notified_at IS NULL OR notified_at='')
              AND status='pending' AND suspicious=0 AND score>=?
            ORDER BY score DESC, price ASC LIMIT ?
            """,
            (min_score, limit),
        )
    )


def mark_notified(conn: sqlite3.Connection, item_ids: set[str] | list[str]) -> int:
    if not item_ids:
        return 0
    stamp = now_text()
    conn.executemany(
        "UPDATE listings SET notified_at=? WHERE item_id=?",
        [(stamp, item_id) for item_id in item_ids],
    )
    conn.commit()
    return len(item_ids)


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
    """最近一轮**已完成**的扫描：未收尾的轮次还没有不符合明细，不能当展示基准。"""
    return conn.execute(
        "SELECT * FROM scans WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()


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


def mark_manual_reject(
    conn: sqlite3.Connection, item_id: str, reason: str = MANUAL_REJECT_REASON
) -> bool:
    """人工忽略：状态置 ignored，同时写进最新一轮的不符合明细。"""
    row = conn.execute(
        "SELECT last_scan_id FROM listings WHERE item_id=?", (item_id,)
    ).fetchone()
    if row is None:
        return False
    scan = conn.execute(
        "SELECT id FROM scans WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    scan_id = int(scan["id"]) if scan else int(row["last_scan_id"] or 0)
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
        (scan_id, reason, item_id),
    )
    conn.execute(
        "UPDATE listings SET status='ignored', note=? WHERE item_id=?",
        (reason, item_id),
    )
    conn.commit()
    return True


def restore_pending(conn: sqlite3.Connection, item_id: str) -> bool:
    """把人工忽略 / 积压重筛移出的商品退回待审核，并清掉对应的不符合记录。"""
    row = conn.execute("SELECT note FROM listings WHERE item_id=?", (item_id,)).fetchone()
    note = str(row["note"] or "").strip() if row else ""
    cursor = conn.execute(
        "UPDATE listings SET status='pending', note='' WHERE item_id=?", (item_id,)
    )
    if cursor.rowcount:
        # 人工忽略记的原因就是 MANUAL_REJECT_REASON；积压重筛记的是「重筛排除：<原因>」
        reasons = {MANUAL_REJECT_REASON}
        if note.startswith(RESCREEN_NOTE_PREFIX):
            reasons.add(note[len(RESCREEN_NOTE_PREFIX) :].strip())
        elif note:
            reasons.add(note)
        conn.execute(
            "DELETE FROM scan_rejects WHERE item_id=? AND reason IN (%s)"
            % ",".join("?" * len(reasons)),
            (item_id, *sorted(reasons)),
        )
    conn.commit()
    return cursor.rowcount > 0


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM listings GROUP BY status")
    return {row["status"]: row["n"] for row in rows}
