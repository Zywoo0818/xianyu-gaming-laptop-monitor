"""演示数据：结构完全模拟闲鱼搜索接口，离线走完粗筛。

`python -m xianyu scan --demo` 不需要登录、不联网。
方案二不打开卖家主页；DemoCrawler 保留仅为兼容旧调用。
"""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any

from .config import Config, Watch
from .models import Listing, SellerItem, SellerPage
from .parse import ApiRejected, ParseError, parse_payload

AREAS = ["上海", "杭州", "深圳", "北京", "广州", "成都", "苏州", "武汉", "南京"]
MODELS = [
    ("联想拯救者R9000P", "R7-5800H"),
    ("华硕天选4", "i7-12700H"),
    ("机械革命蛟龙16K", "R7-7735H"),
]
NORMAL = ("小陈同学", "person")
DJI = ("阿杰", "dji")
PARTS = ("换新回血", "parts")


def _seller_id(seed: int, kind: str) -> str:
    return f"demo{seed}-{kind}"


def _wrapper(
    item_id: str,
    title: str,
    price: float,
    area: str,
    seller: str,
    seller_id: str,
    minutes_ago: int,
    wants: int,
) -> dict[str, Any]:
    stamp = int((time.time() - minutes_ago * 60) * 1000)
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
                        "fishTags": {"r1": {"tagList": []}},
                    },
                    "clickParam": {
                        "args": {
                            "publishTime": str(stamp),
                            "wantNum": str(wants),
                            "seller_id": seller_id,
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


def _build(watch: Watch, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    gpu = _gpu_from_keyword(watch.keyword)
    wrappers: list[dict[str, Any]] = []

    def add(
        title: str,
        price: float,
        minutes: int,
        *,
        seller: str,
        seller_id: str,
    ) -> None:
        wrappers.append(
            _wrapper(
                f"demo{seed}{len(wrappers):03d}",
                title,
                price,
                rng.choice(AREAS),
                seller,
                seller_id,
                minutes,
                rng.randint(0, 120),
            )
        )

    normal_name, normal_kind = NORMAL
    dji_name, dji_kind = DJI
    parts_name, parts_kind = PARTS
    normal_id = _seller_id(seed, normal_kind)

    # 个人卖家：粗筛、细筛都通过
    for index, (name, cpu) in enumerate(MODELS, start=1):
        add(
            f"{name} {cpu} RTX{gpu.upper()} 16G 512G 游戏本 个人自用 无拆修",
            3600 + index * 250,
            25 * index,
            seller=normal_name,
            seller_id=normal_id,
        )

    # 细筛模块 1：卖家其他商品有大疆 → 整个卖家排除
    add(
        f"{MODELS[0][0]} RTX{gpu.upper()} 16G 512G 游戏本 成色好",
        3900,
        45,
        seller=dji_name,
        seller_id=_seller_id(seed, dji_kind),
    )
    add(
        f"{MODELS[1][0]} RTX{gpu.upper()} 16G 512G 游戏本 换新出",
        4700,
        70,
        seller=dji_name,
        seller_id=_seller_id(seed, dji_kind),
    )

    # 细筛模块 2：主页大量电脑配件 → 商家排除
    add(
        f"{MODELS[2][0]} RTX{gpu.upper()} 16G 512G 游戏本",
        4200,
        90,
        seller=parts_name,
        seller_id=_seller_id(seed, parts_kind),
    )

    # 粗筛商家模块 1：卖家昵称就是数码城
    add(
        f"{MODELS[0][0]} RTX{gpu.upper()} 16G 512G 游戏本 大量到货",
        3500,
        110,
        seller="华强北数码城",
        seller_id=_seller_id(seed, "shop"),
    )

    # 粗筛可用性：无法开机
    add(
        f"{MODELS[1][0]} RTX{gpu.upper()} 游戏本 无法开机 当配件出",
        1500,
        130,
        seller=normal_name,
        seller_id=normal_id,
    )

    # 缺卖家 ID：细筛做不了，保留到待审核并标注
    add(
        f"{MODELS[2][0]} RTX{gpu.upper()} 16G 512G 游戏本 个人闲置",
        4300,
        160,
        seller="闲鱼用户8823",
        seller_id="",
    )

    return wrappers


def demo_listings(cfg: Config, watches: list[Watch] | None = None) -> dict[str, list[Listing]]:
    watches = watches or cfg.watches
    result: dict[str, list[Listing]] = {}
    for index, watch in enumerate(watches):
        wrappers = _build(watch, seed=index + 1)
        listings: list[Listing] = []
        for start in range(0, len(wrappers), 10):
            parsed, _ = parse_payload(_payload(wrappers[start : start + 10]))
            listings.extend(parsed)
        for listing in listings:
            listing.watch = watch.name
            listing.keyword = watch.keyword
        result[watch.name] = listings
    return result


def _watch_from_filename(path: Path, watches: list[Watch]) -> Watch:
    match = re.match(r"search_(.+?)_p\d+_\d+\.json$", path.name)
    if match:
        slug = match.group(1)
        for watch in watches:
            safe = re.sub(r'[\\/:*?"<>|\s]+', "_", watch.keyword).strip("_")
            if slug == safe:
                return watch
    return watches[0]


def load_saved_listings(path: Path, watches: list[Watch]) -> dict[str, list[Listing]]:
    """回放 --dump 保存的真实搜索响应；单个文件或整个目录均可。"""
    if not watches:
        raise ValueError("没有配置监控项，无法回放抓包")
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    if not files:
        raise ValueError(f"抓包目录里没有 JSON：{path}")
    result: dict[str, list[Listing]] = {watch.name: [] for watch in watches}
    seen: dict[str, set[str]] = {watch.name: set() for watch in watches}
    for file in files:
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"读取抓包失败：{file.name}（{exc}）") from exc
        try:
            listings, _ = parse_payload(payload)
        except (ParseError, ApiRejected) as exc:
            raise ValueError(f"抓包里没有可解析的搜索数据：{file.name}（{exc}）") from exc
        watch = _watch_from_filename(file, watches)
        for listing in listings:
            if listing.item_id in seen[watch.name]:
                continue
            seen[watch.name].add(listing.item_id)
            listing.watch = watch.name
            listing.keyword = watch.keyword
            result[watch.name].append(listing)
    return result


PARTS_PAGE = [
    "金士顿 8G DDR4 笔记本内存条",
    "三星 512G NVMe 固态硬盘",
    "RTX3060 笔记本显卡 拆机",
    "联想拯救者 主板 维修件",
    "15.6寸 笔记本屏幕 1080P",
    "230W 电源适配器 原装",
]


class DemoCrawler:
    """离线卖家主页：按卖家 ID 后缀返回固定场景。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stats = {"search_requests": 0, "fine_requests": 0}

    def fetch_sellers(self, sellers, *, stop_event=None):
        pages: dict[str, SellerPage] = {}
        errors: dict[str, str] = {}
        for seller in sellers:
            if stop_event is not None and stop_event.is_set():
                break
            self.stats["fine_requests"] += 1
            kind = seller.seller_id.rsplit("-", 1)[-1]
            titles = ["联想拯救者R9000P RTX3060 16G 512G 游戏本"]
            if kind == "dji":
                titles = [
                    "华硕天选4 RTX4060 16G 512G 游戏本",
                    "大疆 Mini 3 Pro 无人机 畅飞套装",
                ]
            elif kind == "parts":
                titles = list(PARTS_PAGE)
            page = SellerPage(
                seller_id=seller.seller_id,
                seller_name=seller.seller_name,
                url=f"https://www.goofish.com/personal?userId={seller.seller_id}",
                in_sale=len(titles) + 3,
                items=[
                    SellerItem(
                        item_id=f"{seller.seller_id}-{index}",
                        title=title,
                        url="",
                    )
                    for index, title in enumerate(titles, start=1)
                ],
            )
            pages[seller.seller_id] = page
        return pages, errors
