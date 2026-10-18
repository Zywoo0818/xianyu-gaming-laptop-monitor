"""关键词命中判断，带否定/正面后缀处理。

「无磕碰」「无维修」不算磕碰和维修；
「主板正常」「屏幕完好」不算故障。
"""
from __future__ import annotations

NEGATIONS = ("无", "没", "不", "非", "未", "免", "别")
POSITIVE_TAILS = ("正常", "完好", "良好", "无问题", "无暗病", "无修", "状态正", "完美", "没坏")


def hit_words(text: str, words: list[str]) -> list[str]:
    lowered = str(text or "").casefold()
    found: list[str] = []
    for word in words:
        needle = str(word or "").casefold()
        if not needle:
            continue
        index = lowered.find(needle)
        while index >= 0:
            prefix = lowered[max(0, index - 3) : index]
            tail = lowered[index + len(needle) : index + len(needle) + 4]
            negated = any(item in prefix for item in NEGATIONS)
            positive = any(item in tail for item in POSITIVE_TAILS)
            if not negated and not positive:
                found.append(word)
                break
            index = lowered.find(needle, index + len(needle))
    return found
