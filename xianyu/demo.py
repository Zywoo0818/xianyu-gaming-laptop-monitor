"""演示数据：结构完全模拟闲鱼搜索接口，报价参考你自己的行情表。

`python -m xianyu scan --demo` 离线看效果，不需要登录、不联网。
"""
from __future__ import annotations

import random
import re
import time
from typing import Any

from .config import Config, Watch
from .models import Listing
from .parse import parse_payload
from .pricetable import PriceRow, load_table

AREAS = ["上海", "杭州", "深圳", "北京", "广州", "成都", "苏州", "武汉", "南京"]
SELLERS = ["闲鱼用户8823", "小陈同学", "换新回血", "阿杰", "林先生", "打游戏的大学生"]
MERCHANTS = ["华强北数码城", "大连华南电子城极客修", "专业回收工作室"]


def _wrapper(
    item_id: str,
    title: str,
    price: float,
    area: str,
    seller: str,
    minutes_ago: int,
    wants: int,
    *,
    verified: bool = False,
    freeship: bool = False,
) -> dict[str, Any]:
    stamp = int((time.time() - minutes_ago * 60) * 1000)
    tags: list[dict[str, Any]] = []
    if verified:
        tags.append({"data": {"content": "验货宝"}})
    return {
        "data": {
            "item": {
                "main": {
                    "exContent": {
                        "itemId": item_id,
                        "title": title,
                        "price": [{"text": str(int(price)), "type": "price"}],
                        "picUrl": "",
                        "area": area,
                        "userNickName": seller,
                        "fishTags": {"r1": {"tagList": tags}},
                    },
                    "clickParam": {
                        "args": {
                            "publishTime": str(stamp),
                            "wantNum": str(wants),
                            "tag": "freeship" if freeship else "",
                        }
                    },
                }
            }
        }
    }


def _payload(wrappers: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "api": "mtop.taobao.idlemtopsearch.pc.search",
        "ret": ["SUCCESS::调用成功"],
        "data": {"resultList": wrappers},
    }


def _gpu_from_keyword(keyword: str) -> str:
    match = re.search(r"(10[5-8]0|16[5-6]0|20[5-8]0|30[5-8]0|40[5-9]0|50[5-9]0)", keyword)
    return match.group(1) if match else "3060"


def _build(watch: Watch, rows: list[PriceRow], seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    gpu = _gpu_from_keyword(watch.keyword)
    pool = [row for row in rows if row.gpu == gpu] or rows[:4]
    wrappers: list[dict[str, Any]] = []

    def add(title: str, price: float, minutes: int, *, seller: str | None = None, **kwargs: Any) -> None:
        wrappers.append(
            _wrapper(
                f"demo{seed}{len(wrappers):03d}",
                title,
                price,
                rng.choice(AREAS),
                seller or rng.choice(SELLERS),
                minutes,
                rng.randint(0, 120),
                **kwargs,
            )
        )

    if not pool:
        return wrappers

    # 真货：比行情表低 150 / 400 / 700
    for row, gap, minutes in zip(pool[:3], (150, 400, 700), (25, 180, 900)):
        add(f"{row.name} {row.cpu} RTX{gpu.upper()} 16G 512G 游戏本 个人自用 无拆修", row.price - gap, minutes)

    # 正常/偏贵：应该被价差底线挡掉
    for row in pool[:3]:
        add(f"{row.name} RTX{gpu.upper()} 16G 512G 游戏本", row.price + rng.randint(0, 400), rng.randint(60, 3000))

    # 商家
    add(
        f"{pool[0].name} RTX{gpu.upper()} 16G 512G 游戏本 大量到货",
        pool[0].price - 200,
        300,
        seller=rng.choice(MERCHANTS),
    )
    # 想要太多
    add(f"{pool[0].name} RTX{gpu.upper()} 游戏本 成色好", pool[0].price - 300, 200)
    wrappers[-1]["data"]["item"]["main"]["clickParam"]["args"]["wantNum"] = "160"

    # 骗子/异常：定金、进水、账号锁
    add(f"{pool[0].name} RTX{gpu.upper()} 定金 500 先到先得", pool[0].price * 0.35, 40)
    add(f"{pool[0].name} 进水 黑屏 会修的来 RTX{gpu.upper()}", pool[0].price * 0.45, 90)
    add(f"{pool[0].name} RTX{gpu.upper()} 有账号锁 便宜出", pool[0].price - 600, 120)

    # 该被品类闸门排除的：轻薄本、苹果、工作站、无 N 卡
    add("联想小新 Air14 轻薄本 办公本 无独显", 1800, 60)
    add("MacBook Pro 14 M1 Pro 16G 512G", 4200, 90)
    add("华为 MateBook 14 轻薄本", 2600, 120)
    add("移动工作站 ThinkPad P15v RTX A2000 图形卡", 3800, 150)
    add(f"{pool[0].name} 保护壳 键盘膜 全套", 59, 200)
    add("求购 游戏本 一台 价格好说", 2000, 300)
    return wrappers


def demo_listings(cfg: Config) -> dict[str, list[Listing]]:
    rows = load_table(cfg.price_table.path) if cfg.price_table else []
    watches = cfg.watches[:3] if len(cfg.watches) >= 3 else cfg.watches
    result: dict[str, list[Listing]] = {}
    for index, watch in enumerate(watches):
        wrappers = _build(watch, rows, seed=index + 1)
        listings: list[Listing] = []
        for chunk in (wrappers[:10], wrappers[10:]):
            if not chunk:
                continue
            parsed, _ = parse_payload(_payload(chunk))
            listings.extend(parsed)
        for listing in listings:
            listing.watch = watch.name
            listing.keyword = watch.keyword
        result[watch.name] = listings
    return result
