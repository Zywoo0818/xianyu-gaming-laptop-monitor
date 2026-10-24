"""品牌识别与权重。

你的要求：苹果、华为直接排除；华硕、机械革命、联想、微星、神舟、七彩虹优先；其他一般。
"""
from __future__ import annotations

from dataclasses import dataclass

EXCLUDED = {
    "苹果": ["macbook", "mac book", "苹果", "imac", "mac mini", "mac air", "mac pro"],
    "华为": ["华为", "matebook", "mate book"],
}

# 优先品牌：关键词 -> 标准名
PRIORITY = {
    "华硕": ["华硕", "asus", "rog", "玩家国度", "天选", "飞行堡垒", "魔霸", "枪神", "幻14", "幻16"],
    "机械革命": ["机械革命", "机革", "蛟龙", "极光", "钛钽", "旷世"],
    "联想": ["联想", "拯救者", "legion", "r9000", "y9000", "geekpro", "刃"],
    "微星": ["微星", "msi", "龙盾", "gp75", "ge62", "ge75"],
    "神舟": ["神舟", "hasee", "战神", "炫龙", "tx9", "z8"],
    "七彩虹": ["七彩虹", "colorful", "将星", "隐星"],
}

# 其他常见游戏本品牌，权重 0
OTHER = {
    "惠普": ["惠普", "hp", "暗影精灵", "光影精灵", "omen"],
    "戴尔": ["戴尔", "dell", "游匣", "g15", "g16", "外星人", "alienware"],
    "宏碁": ["宏碁", "acer", "暗影骑士", "掠夺者", "predator", "nitro"],
    "雷神": ["雷神", "thunderobot", "911"],
    "机械师": ["机械师", "machenike", "创物者", "曙光", " commander"],
    "未来人类": ["未来人类", "terrans force", "s16", "x511"],
    "技嘉": ["技嘉", "gigabyte", "aorus", "aero"],
    "火影": ["火影", "t5a", "t6a"],
    "荣耀": ["荣耀", "honor", "magicbook"],
}

PRIORITY_WEIGHT = {
    "华硕": 8,
    "机械革命": 8,
    "联想": 8,
    "微星": 6,
    "神舟": 6,
    "七彩虹": 6,
}


@dataclass
class BrandInfo:
    name: str
    weight: float = 0.0
    excluded: bool = False
    priority: bool = False


def detect(title: str, seller: str = "") -> BrandInfo:
    text = f"{title} {seller}".casefold()
    for name, words in EXCLUDED.items():
        if any(word.casefold() in text for word in words):
            return BrandInfo(name=name, weight=-100, excluded=True)
    for name, words in PRIORITY.items():
        if any(word.casefold() in text for word in words):
            return BrandInfo(name=name, weight=PRIORITY_WEIGHT[name], priority=True)
    for name, words in OTHER.items():
        if any(word.casefold() in text for word in words):
            return BrandInfo(name=name, weight=0.0)
    return BrandInfo(name="其他", weight=0.0)
