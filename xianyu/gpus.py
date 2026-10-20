"""英伟达显卡识别与加权。

规则来自你的要求：
- 必须是 N 卡；
- 1060 以下（1050 / 1050Ti / MX 系列）大幅减分；
- 20/30/40 代加分；
- 2060 / 3050 / 3060 / 4050 / 4060 重点加分；
- 50 代减分。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

GPU_PATTERN = re.compile(
    r"(?<!\d)(10[5-8]0|16[5-6]0|20[5-8]0|30[5-8]0|40[5-9]0|50[5-9]0)\s*(ti|super)?(?!\d)",
    re.IGNORECASE,
)

# 「1080」既是显卡也是屏幕分辨率：1080P / 1920x1080 里的那个不是显卡。
RESOLUTION_TAIL = re.compile(r"[pP](?![a-zA-Z])")
RESOLUTION_HEAD = re.compile(r"\d\s*[x×*]\s*$", re.IGNORECASE)

WORKSTATION_PATTERN = re.compile(
    r"quadro|rtx\s*a\d{4}|(?<![a-z])a[2-5]000|t1000|t2000|p[0-9]{4}|w[0-9]{4}|专业卡|图形卡",
    re.IGNORECASE,
)

AMD_PATTERN = re.compile(r"(?<![a-z])rx\s?\d{3,4}|radeon", re.IGNORECASE)

# 单卡得分（命中即用，不再叠加 series）
MODEL_SCORE = {
    "2060": 12,
    "3050": 10,
    "3050ti": 10,
    "3060": 14,
    "3060ti": 14,
    "4050": 12,
    "4060": 15,
    "4060ti": 15,
    "2070": 6,
    "2080": 6,
    "3070": 7,
    "3070ti": 7,
    "3080": 7,
    "3080ti": 7,
    "4070": 7,
    "4070ti": 7,
    "4080": 7,
    "4080ti": 7,
    "4090": 7,
    "2050": 0,
    "1660": 4,
    "1660ti": 4,
    "1650": -6,
    "1650ti": -6,
    "1060": 0,
    "1070": 3,
    "1080": 3,
    "1050": -25,
    "1050ti": -25,
    "5050": -10,
    "5060": -10,
    "5070": -10,
    "5080": -10,
    "5090": -10,
}

WEAK_MODELS = {"1050", "1050ti", "1650", "1650ti"}


@dataclass
class GpuInfo:
    model: str
    series: int
    score: float
    is_nvidia: bool = True
    is_workstation: bool = False

    @property
    def weak(self) -> bool:
        return self.model in WEAK_MODELS


def _is_resolution(text: str, match: re.Match) -> bool:
    end = match.end()
    if match.group(2) is None:
        end = match.end(1)
    if RESOLUTION_TAIL.match(text[end:].lstrip()):
        return True
    return bool(RESOLUTION_HEAD.search(text[: match.start(1)]))


def detect(title: str) -> GpuInfo | None:
    text = str(title or "")
    if WORKSTATION_PATTERN.search(text):
        return GpuInfo("专业卡", 0, -40, is_nvidia=True, is_workstation=True)
    # 逐个候选取第一个真显卡：标题里的「1080P」要跳过，后面的 3060 才算
    match = next(
        (found for found in GPU_PATTERN.finditer(text) if not _is_resolution(text, found)),
        None,
    )
    if match is None:
        if AMD_PATTERN.search(text):
            return GpuInfo("AMD", 0, -40, is_nvidia=False)
        return None
    model = match.group(1) + (match.group(2) or "").lower()
    series = int(model[:2])

    # 优先精确匹配（比如 3060ti），如果字典里没有，则尝试剥掉后缀匹配（比如 3080super 退回 3080）
    if model in MODEL_SCORE:
        score = MODEL_SCORE[model]
    else:
        base_model = model.replace("ti", "").replace("super", "")
        if base_model in MODEL_SCORE:
            score = MODEL_SCORE[base_model]
        else:
            score = {10: 0, 16: 2, 20: 5, 30: 6, 40: 6, 50: -10}.get(series, 0)

    return GpuInfo(model=model.upper().replace("TI", "Ti"), series=series, score=float(score))


def describe(gpu: GpuInfo | None) -> str:
    if gpu is None:
        return "无独显"
    if gpu.is_workstation:
        return "专业卡(工作站)"
    if not gpu.is_nvidia:
        return "非 N 卡"
    return f"RTX/GTX {gpu.model}"
