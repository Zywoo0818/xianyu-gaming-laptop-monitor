"""按天记录每轮扫描的结构化摘要：请求数、风控次数、耗时、结果。"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .config import Config


def log_scan(
    cfg: Config,
    *,
    status: str,
    elapsed: float,
    requests: dict[str, int] | None = None,
    search_risk: int = 0,
    detail_risk: int = 0,
    soft_failures: int = 0,
    outcome: Any | None = None,
    error: str = "",
) -> Path:
    requests = requests or {}
    search_requests = int(requests.get("search_requests", 0))
    detail_requests = int(requests.get("detail_requests", 0))
    total_requests = search_requests + detail_requests
    day = time.strftime("%Y-%m-%d")
    path = cfg.root / "logs" / f"scan-{day}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        time.strftime("%Y-%m-%d %H:%M:%S"),
        f"status={status}",
        f"requests={total_requests}",
        f"search_requests={search_requests}",
        f"detail_requests={detail_requests}",
        f"risk_search={search_risk}",
        f"risk_detail={detail_risk}",
        f"soft={soft_failures}",
        f"elapsed={elapsed:.1f}s",
    ]
    if outcome is not None:
        fields.extend(
            [
                f"scan={getattr(outcome, 'scan_id', '-')}",
                f"fetched={outcome.stats.get('fetched', 0)}",
                f"kept={outcome.stats.get('kept', 0)}",
            ]
        )
    if error:
        fields.append("error=" + " ".join(str(error).split())[:200])
    with path.open("a", encoding="utf-8") as handle:
        handle.write(" ".join(fields) + "\n")
    return path
