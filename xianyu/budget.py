"""每日搜索请求预算：落盘到 data/request_budget.json，跨进程累计。

计数口径是“实际发出的搜索接口请求”，不是“成功解析的响应”：
响应超时、返回非 JSON、被浏览器丢弃的请求也要算进去。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def budget_path(data_dir: Path) -> Path:
    return data_dir / "request_budget.json"


class DailyBudget:
    def __init__(self, path: Path, limit: int):
        self.path = path
        self.limit = max(0, int(limit))
        self.day, self.used = self._load()

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _load(self) -> tuple[str, int]:
        today = self._today()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            day = str(raw.get("day") or "")
            used = max(0, int(raw.get("used") or 0))
        except (OSError, ValueError, TypeError):
            return today, 0
        if day != today:
            return today, 0
        return day, used

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"day": self.day, "used": self.used}, ensure_ascii=False)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, count: int) -> None:
        today = self._today()
        if today != self.day:
            self.day, self.used = today, 0
        delta = max(0, int(count))
        if not delta:
            return
        self.used += delta
        self._save()

    def exhausted(self) -> bool:
        return self.limit > 0 and self.used >= self.limit

    def remaining(self) -> int | None:
        if self.limit <= 0:
            return None
        return max(0, self.limit - self.used)

    def describe(self) -> str:
        if self.limit <= 0:
            return f"每日请求预算：不限（今日已发 {self.used}）"
        return f"每日请求预算：{self.used}/{self.limit}（{self.day}）"
