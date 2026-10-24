"""风控熔断：人机验证、频率限制、软失败各走各的梯子。

分档冷却，是因为这几件事的恢复方式根本不同：

- **人机验证 / 处罚**（搜索风控）：需要主人手动过验证，连续撞只会加重账号风险，
  所以按天起步（`scan.cooldown_minutes` 1440，最高 7 天）；
- **频率限制**（`请求频率或额度已超限`、ret 里的 TRAFFIC/RATE_LIMIT）：闲鱼自己会解除，
  有头登录态下等几十分钟就够，走 `scan.rate_limit_cooldown_minutes` 30 分起、
  最高 12 小时的短梯子；
- **软失败**（超时、结构异常、可能改版）：多半是页面改版而不是风控，走小时级，
  冷却上限压到 24 小时以内，并把 `xianyu/selectors.py` 指给你，不用干等；
- **登录失效**（cookie 组缺失、跳登录页、接口要求登录）不计入熔断：等多久都不会
  自己恢复，应该立刻停下去运行 login.bat。

其余规则：
- 只有一整轮完全干净（所有关键词跑完、有数据、没有任何错误）才清零；
- 部分成功不清零，避免一次侥幸拿到几条就把连续风控掩盖掉；
- 冷却到期只清连续计数，不清各档升级等级，再次触发在原等级上继续翻倍；
- 各档独立等级，限流把风控梯子顶到 48 小时是没有道理的；
- 方案二登录态是主人的账号：任何一档触发后都不自动重试。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Callable, Iterable

from . import selectors as sel
from .config import Config

# 「非法访问」也归到风控档：它要人工处理或换 UA，等着不会自己好。
RISK_MARKERS = ("风控", "人机验证") + sel.RISK_TEXT_MARKERS + sel.ILLEGAL_ACCESS_MARKERS
LOGIN_MARKERS = ("需要登录", "登录态", "登录后", "扫码登录")

ERROR_GROUPS = ("search_risk", "fine_risk", "rate_limit", "soft")


def is_rate_limit(message: str) -> bool:
    """是否属于「等一会儿就能恢复」的频率/额度限制。"""
    return bool(sel.first_match(message, sel.RATE_LIMIT_ANY_MARKERS))


def is_login_error(message: str) -> bool:
    """登录态失效：不计数、不冷却，立刻停下重新登录。"""
    return any(marker in str(message) for marker in LOGIN_MARKERS)


def is_risk_error(message: str) -> bool:
    """是否属于需要人工处理的风控；限流、登录失效不算。"""
    if is_rate_limit(message) or is_login_error(message):
        return False
    return any(marker in message for marker in RISK_MARKERS)


def error_kind(message: str) -> str:
    """错误归类：先登录，再限流，再风控，其余算软失败（含改版）。"""
    text = str(message or "")
    if is_login_error(text):
        return "login"
    if is_rate_limit(text):
        return "rate_limit"
    if is_risk_error(text):
        return "risk"
    return "soft"


def classify_errors(errors: dict[str, str]) -> dict[str, list[str]]:
    """把一轮错误拆成搜索风控、细筛风控、频率限制、软失败四组。

    登录失效单独跳过：它由调用方立刻停机处理，不该推进熔断计数。
    """
    groups: dict[str, list[str]] = {key: [] for key in ERROR_GROUPS}
    for name, raw in errors.items():
        message = str(raw)
        kind = error_kind(message)
        if kind == "login":
            continue
        if kind == "rate_limit":
            groups["rate_limit"].append(message)
        elif kind == "risk":
            is_fine = str(name) == "细筛" or message.startswith("细筛")
            groups["fine_risk" if is_fine else "search_risk"].append(message)
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

    def _defaults(self) -> dict:
        return {
            "search_failures": 0,
            "fine_failures": 0,
            "rate_limit_failures": 0,
            "soft_failures": 0,
            "trip_level": 0,
            "rate_level": 0,
            "soft_level": 0,
            "paused_level": 0,
            "paused_until": 0.0,
            "paused_at": 0.0,
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
            state["search_failures"] = max(
                0, int(raw.get("search_failures", raw.get("failures") or 0))
            )
            # 兼容从 v2 数据库目录直接升级过来的状态文件。
            state["fine_failures"] = max(
                0, int(raw.get("fine_failures", raw.get("detail_failures") or 0))
            )
            state["rate_limit_failures"] = max(0, int(raw.get("rate_limit_failures") or 0))
            state["soft_failures"] = max(0, int(raw.get("soft_failures") or 0))
            state["trip_level"] = max(0, int(raw.get("trip_level") or 0))
            state["rate_level"] = max(0, int(raw.get("rate_level") or 0))
            state["soft_level"] = max(0, int(raw.get("soft_level") or 0))
            state["paused_level"] = max(0, int(raw.get("paused_level") or 0))
            state["paused_until"] = max(0.0, float(raw.get("paused_until") or 0))
            state["paused_at"] = max(0.0, float(raw.get("paused_at") or 0))
        except (TypeError, ValueError):
            return self._defaults()
        # 只有分档等级之前的旧状态文件才需要反推 trip_level：那时 paused_at 只可能由风控产生。
        legacy = not any(key in raw for key in ("rate_level", "soft_level", "paused_level"))
        if legacy and state["trip_level"] <= 0 and state["paused_at"] > 0:
            state["trip_level"] = 1
        if not state["paused_level"]:
            state["paused_level"] = max(
                state["trip_level"], state["rate_level"], state["soft_level"]
            )
        state["last_error"] = str(raw.get("last_error") or "")[:200]
        state["trip_reason"] = str(raw.get("trip_reason") or "")[:120]
        state["updated_at"] = str(raw.get("updated_at") or "")
        return state

    def _save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def _cooldown_seconds(self, base_minutes: int, max_minutes: int, level: int) -> float:
        base = max(1, int(base_minutes)) * 60
        cap = max(base, int(max_minutes) * 60)
        return min(cap, base * (2 ** max(0, level - 1)))

    def status(self) -> dict:
        """当前状态；冷却到期自动清连续计数，但保留各档熔断等级。"""
        state = self._load()
        now = time.time()
        changed = False
        if state["paused_until"] and state["paused_until"] <= now:
            state["search_failures"] = 0
            state["fine_failures"] = 0
            state["rate_limit_failures"] = 0
            state["soft_failures"] = 0
            state["paused_until"] = 0.0
            state["paused_at"] = 0.0
            state["updated_at"] = _now_text()
            changed = True
        if changed:
            self._save(state)
        return state

    def should_skip(self) -> tuple[bool, str]:
        if (
            self.cfg.scan.failure_threshold <= 0
            and self.cfg.scan.fine_failure_threshold <= 0
            and self.cfg.scan.soft_failure_threshold <= 0
            and self.cfg.scan.rate_limit_threshold <= 0
        ):
            return False, ""
        state = self.status()
        remaining = state["paused_until"] - time.time()
        if remaining <= 0:
            return False, ""
        until = datetime.fromtimestamp(state["paused_until"]).strftime("%m-%d %H:%M")
        reason = state["trip_reason"] or "连续失败"
        level = state["paused_level"] or state["trip_level"] or 1
        return True, (
            f"风控冷却中：{reason}（第 {level} 次熔断），"
            f"暂停到 {until}（还剩 {_format_duration(remaining)}）。"
            "确认已手动处理验证后，可用 scan.bat --force 强跑一次"
        )

    def record_clean(self) -> None:
        """一整轮完全干净：计数和各级等级全部归零。"""
        state = self._load()
        if not any(
            state[key]
            for key in (
                "search_failures",
                "fine_failures",
                "rate_limit_failures",
                "soft_failures",
                "paused_until",
                "trip_level",
                "rate_level",
                "soft_level",
                "paused_level",
            )
        ):
            return
        state = self._defaults()
        state["updated_at"] = _now_text()
        self._save(state)

    def reset(self) -> None:
        state = self._defaults()
        state["updated_at"] = _now_text()
        self._save(state)

    def _trip_hits(self, state: dict) -> list[tuple[str, int, int, str]]:
        """返回已达标的档位：(名称, 冷却下限分钟, 上限分钟, 等级字段)。"""
        hits: list[tuple[str, int, int, str]] = []
        scan = self.cfg.scan
        if scan.failure_threshold > 0 and state["search_failures"] >= scan.failure_threshold:
            hits.append(("搜索风控", scan.cooldown_minutes, scan.cooldown_max_minutes, "trip_level"))
        if (
            scan.fine_failure_threshold > 0
            and state["fine_failures"] >= scan.fine_failure_threshold
        ):
            hits.append(("细筛风控", scan.cooldown_minutes, scan.cooldown_max_minutes, "trip_level"))
        if (
            scan.rate_limit_threshold > 0
            and state["rate_limit_failures"] >= scan.rate_limit_threshold
        ):
            hits.append(
                (
                    "频率限制",
                    scan.rate_limit_cooldown_minutes,
                    scan.rate_limit_max_minutes,
                    "rate_level",
                )
            )
        if scan.soft_failure_threshold > 0 and state["soft_failures"] >= scan.soft_failure_threshold:
            hits.append(
                ("软失败", scan.soft_cooldown_minutes, scan.soft_cooldown_max_minutes, "soft_level")
            )
        return hits

    def observe(
        self,
        *,
        clean: bool,
        search_errors: Iterable[str] = (),
        fine_errors: Iterable[str] = (),
        rate_limit_errors: Iterable[str] = (),
        soft_errors: Iterable[str] = (),
        logger: Callable[[str], None] = print,
    ) -> bool:
        """一轮扫描结束后调用；只有 clean=True 才清零。"""
        search_errors = [str(message) for message in search_errors]
        fine_errors = [str(message) for message in fine_errors]
        rate_limit_errors = [str(message) for message in rate_limit_errors]
        soft_errors = [str(message) for message in soft_errors]

        if clean:
            self.record_clean()
            return False
        if not (search_errors or fine_errors or rate_limit_errors or soft_errors):
            return False

        state = self.status()
        if search_errors:
            state["search_failures"] += 1
        if fine_errors:
            state["fine_failures"] += 1
        if rate_limit_errors:
            state["rate_limit_failures"] += 1
        if soft_errors:
            state["soft_failures"] += 1

        messages = search_errors + fine_errors + rate_limit_errors + soft_errors
        state["last_error"] = "; ".join(messages)[:200]
        tripped = False
        hits = self._trip_hits(state)
        if hits:
            longest = 0.0
            # 搜索风控和细筛风控共用一条梯子，一轮只升一级，不能各加一次。
            for level_key in dict.fromkeys(level_key for *_rest, level_key in hits):
                state[level_key] += 1
            for reason, base, cap, level_key in hits:
                longest = max(longest, self._cooldown_seconds(base, cap, state[level_key]))
            now = time.time()
            # 冷却期内再次触发只延长不缩短：--force 强跑后不能把原冷却覆盖掉。
            state["paused_until"] = max(float(state["paused_until"]), now + longest)
            state["paused_at"] = now
            state["trip_reason"] = "、".join(reason for reason, *_rest in hits)
            state["paused_level"] = max(
                state["trip_level"], state["rate_level"], state["soft_level"]
            )
            until = datetime.fromtimestamp(state["paused_until"]).strftime("%m-%d %H:%M")
            logger(
                f"  ⚠ {state['trip_reason']}达到阈值，暂停抓取到 {until}"
                f"（第 {state['paused_level']} 次熔断）"
            )
            if state["trip_reason"] == "频率限制":
                logger("  · 只是被限流：等冷却过了再手动跑一轮即可，不用等一天")
            tripped = True
        else:
            logger(
                "  · 熔断计数："
                f"搜索 {state['search_failures']}/{self.cfg.scan.failure_threshold}，"
                f"细筛 {state['fine_failures']}/{self.cfg.scan.fine_failure_threshold}，"
                f"频率 {state['rate_limit_failures']}/{self.cfg.scan.rate_limit_threshold}，"
                f"软失败 {state['soft_failures']}/{self.cfg.scan.soft_failure_threshold}"
            )
        state["updated_at"] = _now_text()
        self._save(state)
        return tripped

    def describe(self) -> str:
        state = self.status()
        if state["paused_until"] > time.time():
            until = datetime.fromtimestamp(state["paused_until"]).strftime("%m-%d %H:%M")
            level = state["paused_level"] or state["trip_level"] or 1
            return (
                f"风控冷却到 {until}（{state['trip_reason'] or '连续失败'}，"
                f"第 {level} 次熔断）"
            )
        parts = [
            f"搜索风控 {state['search_failures']}/{self.cfg.scan.failure_threshold}",
            f"细筛风控 {state['fine_failures']}/{self.cfg.scan.fine_failure_threshold}",
            f"频率限制 {state['rate_limit_failures']}/{self.cfg.scan.rate_limit_threshold}",
            f"软失败 {state['soft_failures']}/{self.cfg.scan.soft_failure_threshold}",
        ]
        next_level = max(state["trip_level"], state["rate_level"], state["soft_level"])
        if next_level:
            parts.append(f"下次熔断按第 {next_level + 1} 级")
        return "风控熔断：" + "，".join(parts)
