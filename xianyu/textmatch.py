"""关键词命中判断，带否定/正面后缀处理。

「无磕碰」「无维修」「无第三方维修」不算磕碰和维修；
「主板正常」「屏幕完好」不算故障。
"""
from __future__ import annotations

import re

NEGATIONS = ("无", "没", "不", "非", "未", "免", "别")
POSITIVE_TAILS = ("正常", "完好", "良好", "无问题", "无暗病", "无修", "状态正", "完美", "没坏", "齐全")
_SEPARATORS = "，,。；;、！!？?|/ \t\n"
_SPLIT = re.compile(f"[{re.escape(_SEPARATORS)}]")


def _prefix(text: str, index: int) -> str:
    """取词前面的那一段，遇到标点或空格就截断。

    窗口要够宽才装得下「无第三方维修」；又必须被分隔符挡住，
    否则「无拆修，维修过」里的维修会被前面的无吞掉。
    """
    segment = _SPLIT.split(text[:index])[-1]
    return segment[-6:]


def hit_words(
    text: str,
    words: list[str],
    *,
    blocked_tails: tuple[str, ...] = (),
) -> list[str]:
    """命中词表；blocked_tails 用来放过「无硬盘位」这类限定词。"""
    lowered = str(text or "").casefold()
    found: list[str] = []
    for word in words:
        needle = str(word or "").casefold()
        if not needle:
            continue
        index = lowered.find(needle)
        while index >= 0:
            after = index + len(needle)
            if blocked_tails and lowered.startswith(blocked_tails, after):
                index = lowered.find(needle, after)
                continue
            prefix = _prefix(lowered, index)
            tail = lowered[after : after + 4]
            negated = any(item in prefix for item in NEGATIONS)
            positive = any(item in tail for item in POSITIVE_TAILS)
            if not negated and not positive:
                found.append(word)
                break
            index = lowered.find(needle, after)
    return found
