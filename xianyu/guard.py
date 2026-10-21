"""风控熔断：搜索风控、详情风控、软失败分别计数。

规则：
- 只有一整轮完全干净（所有关键词跑完、有数据、没有任何错误）才清零；
- 部分成功不清零，避免一次侥幸拿到几条就把连续风控掩盖掉；
- 冷却到期只清连续计数，不清升级等级，再次触发会翻倍升级；
- 刚熔断后重新登录不会立即解除，至少保留一段最短冷静期。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Callable, Iterable

from .config import Config

RISK_MARKERS = ("风控", "人机验证", "安全验证", "滑动验证", "非法访问")
LOGIN_MARKERS = ("需要登录", "未登录", "登录态")


def is_risk_error(message: str) -> bool:
    return any(marker in message for marker in RISK_MARKERS)


def is_login_error(message: str) -> bool:
    return any(marker in message for marker in LOGIN_MARKERS)


def classify_errors(errors: dict[str, str]) -> dict[str, list[str]]:
    """把一轮错误拆成搜索风控、详情风控、软失败三组。"""
    groups: dict[str, list[str]] = {"search_risk": [], "detail_risk": [], "soft": []}
    for name, raw in errors.items():
        message = str(raw)
        if is_risk_error(message):
            key = "detail_risk" if str(name) == "详情" or message.startswith("详情") else "search_risk"
            groups[key].append(message)
        elif is_login_error(message):
            continue
        else:
            groups["soft"].append(message)
    return groups


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _format_duration(seconds: float) -> str:
    minutes = max(1, int(seconds // 60))
    if minutes >= 60:
        hours, rest = divmod(minutes, 60)
        return f"{hours} 小时" + (f" {rest} 分钟" if rest else "")
    return f"{minutes} 分钟"


class RiskGuard:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.path = cfg.data_dir / "risk_guard.json"
        self.login_path = cfg.data_dir / "login_state.json"

    def _defaults(self) -> dict:
        return {
            "search_failures": 0,
            "detail_failures": 0,
            "soft_failures": 0,
            "trip_level": 0,
            "paused_until": 0.0,
            "paused_at": 0.0,
            "login_quiet_until": 0.0,
            "last_error": "",
            "trip_reason": "",
            "updated_at": "",
        }

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._defaults()
        if not isinstance(raw, dict):
            return self._defaults()
        state = self._defaults()
        try:
            # 兼容 v2.2 的 failures 字段。
            state["search_failures"] = max(
                0, int(raw.get("search_failures", raw.get("failures") or 0))
            )
            state["detail_failures"] = max(0, int(raw.get("detail_failures") or 0))
            state["soft_failures"] = max(0, int(raw.get("soft_failures") or 0))
            state["trip_level"] = max(0, int(raw.get("trip_level") or 0))
            state["paused_until"] = max(0.0, float(raw.get("paused_until") or 0))
            state["paused_at"] = max(0.0, float(raw.get("paused_at") or 0))
            state["login_quiet_until"] = max(0.0, float(raw.get("login_quiet_until") or 0))
        except (TypeError, ValueError):
            return self._defaults()
        if state["trip_level"] <= 0 and state["paused_at"] > 0:
            state["trip_level"] = 1
        state["last_error"] = str(raw.get("last_error") or "")[:200]
        state["trip_reason"] = str(raw.get("trip_reason") or "")[:120]
        state["updated_at"] = str(raw.get("updated_at") or "")
        return state

    def _save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def _login_mtime(self) -> float:
        try:
            return self.login_path.stat().st_mtime
        except OSError:
            return 0.0

    def _cooldown_seconds(self, base_minutes: int, max_minutes: int, level: int) -> float:
        base = max(1, int(base_minutes)) * 60
        cap = max(base, int(max_minutes) * 60)
        return min(cap, base * (2 ** max(0, level - 1)))

    def status(self) -> dict:
        """当前状态；冷却到期自动清连续计数，但保留熔断等级。"""
        state = self._load()
        now = time.time()
        changed = False

        # 熔断后重新登录：不清零，只把冷却缩短到“登录时间 + 最短冷静期”。
        if state["paused_until"] > now and state["paused_at"] > 0:
            login_ts = self._login_mtime()
            if (
                login_ts > state["paused_at"]
                and state["login_quiet_until"] <= 0
            ):
                quiet_minutes = max(1, self.cfg.scan.login_quiet_minutes)
                quiet_until = login_ts + quiet_minutes * 60
                if state["paused_until"] > quiet_until:
                    state["paused_until"] = quiet_until
                state["login_quiet_until"] = quiet_until
                state["updated_at"] = _now_text()
                changed = True

        if state["paused_until"] and state["paused_until"] <= now:
            state["search_failures"] = 0
            state["detail_failures"] = 0
            state["soft_failures"] = 0
            state["paused_until"] = 0.0
            state["paused_at"] = 0.0
            state["login_quiet_until"] = 0.0
            state["updated_at"] = _now_text()
            changed = True

        if changed:
            self._save(state)
        return state

    def should_skip(self) -> tuple[bool, str]:
        if (
            self.cfg.scan.failure_threshold <= 0
            and self.cfg.scan.detail_failure_threshold <= 0
            and self.cfg.scan.soft_failure_threshold <= 0
        ):
            return False, ""
        state = self.status()
        remaining = state["paused_until"] - time.time()
        if remaining <= 0:
            return False, ""
        until = datetime.fromtimestamp(state["paused_until"]).strftime("%m-%d %H:%M")
        reason = state["trip_reason"] or "连续失败"
        return True, (
            f"风控冷却中：{reason}（第 {state['trip_level']} 次熔断），"
            f"暂停到 {until}（还剩 {_format_duration(remaining)}）。"
            "重新登录不会立即解除，仍需等过最短冷静期；"
            "确认已手动处理验证后可用 scan.bat --force 强跑一次"
        )

    def record_clean(self) -> None:
        """一整轮完全干净：计数和升级等级全部归零。"""
        state = self._load()
        if (
            not state["search_failures"]
            and not state["detail_failures"]
            and not state["soft_failures"]
            and not state["paused_until"]
            and state["trip_level"] <= 0
        ):
            return
        state = self._defaults()
        state["updated_at"] = _now_text()
        self._save(state)

    def reset(self) -> None:
        state = self._defaults()
        state["updated_at"] = _now_text()
        self._save(state)

    def _trip_hits(self, state: dict) -> list[tuple[str, int, int]]:
        hits: list[tuple[str, int, int]] = []
        scan = self.cfg.scan
        if scan.failure_threshold > 0 and state["search_failures"] >= scan.failure_threshold:
            hits.append(("搜索风控", scan.cooldown_minutes, scan.cooldown_max_minutes))
        if (
            scan.detail_failure_threshold > 0
            and state["detail_failures"] >= scan.detail_failure_threshold
        ):
            hits.append(("详情风控", scan.cooldown_minutes, scan.cooldown_max_minutes))
        if scan.soft_failure_threshold > 0 and state["soft_failures"] >= scan.soft_failure_threshold:
            hits.append(("软失败", scan.soft_cooldown_minutes, scan.soft_cooldown_max_minutes))
        return hits

    def observe(
        self,
        *,
        clean: bool,
        search_errors: Iterable[str] = (),
        detail_errors: Iterable[str] = (),
        soft_errors: Iterable[str] = (),
        logger: Callable[[str], None] = print,
    ) -> bool:
        """一轮扫描结束后调用；只有 clean=True 才清零。"""
        search_errors = [str(message) for message in search_errors]
        detail_errors = [str(message) for message in detail_errors]
        soft_errors = [str(message) for message in soft_errors]

        if clean:
            self.record_clean()
            return False
        if not (search_errors or detail_errors or soft_errors):
            return False

        state = self.status()
        if search_errors:
            state["search_failures"] += 1
        if detail_errors:
            state["detail_failures"] += 1
        if soft_errors:
            state["soft_failures"] += 1

        messages = search_errors + detail_errors + soft_errors
        state["last_error"] = "; ".join(messages)[:200]
        tripped = False
        hits = self._trip_hits(state)
        if hits:
            state["trip_level"] += 1
            level = state["trip_level"]
            seconds = max(
                self._cooldown_seconds(base, cap, level) for _reason, base, cap in hits
            )
            now = time.time()
            state["paused_until"] = now + seconds
            state["paused_at"] = now
            state["login_quiet_until"] = 0.0
            state["trip_reason"] = "、".join(reason for reason, _base, _cap in hits)
            until = datetime.fromtimestamp(state["paused_until"]).strftime("%m-%d %H:%M")
            logger(
                f"  ⚠ {state['trip_reason']}达到阈值，暂停抓取到 {until}"
                f"（第 {level} 次熔断，重新登录也不会立即解除）"
            )
            tripped = True
        else:
            logger(
                "  · 熔断计数："
                f"搜索 {state['search_failures']}/{self.cfg.scan.failure_threshold}，"
                f"详情 {state['detail_failures']}/{self.cfg.scan.detail_failure_threshold}，"
                f"软失败 {state['soft_failures']}/{self.cfg.scan.soft_failure_threshold}"
            )
        state["updated_at"] = _now_text()
        self._save(state)
        return tripped

    def describe(self) -> str:
        state = self.status()
        if state["paused_until"] > time.time():
            until = datetime.fromtimestamp(state["paused_until"]).strftime("%m-%d %H:%M")
            return (
                f"风控冷却到 {until}（{state['trip_reason'] or '连续失败'}，"
                f"第 {state['trip_level']} 次熔断）"
            )
        parts = [
            f"搜索风控 {state['search_failures']}/{self.cfg.scan.failure_threshold}",
            f"详情风控 {state['detail_failures']}/{self.cfg.scan.detail_failure_threshold}",
            f"软失败 {state['soft_failures']}/{self.cfg.scan.soft_failure_threshold}",
        ]
        if state["trip_level"]:
            parts.append(f"下次熔断按第 {state['trip_level'] + 1} 级")
        return "风控熔断：" + "，".join(parts)
