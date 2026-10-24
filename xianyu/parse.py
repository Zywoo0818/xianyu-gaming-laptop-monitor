"""解析闲鱼 PC 搜索接口返回的 JSON。

接口：闲鱼 PC 搜索（mtop 名称见 selectors.SEARCH_API）
结构：data.resultList[].data.item.main.exContent
      itemId / title / price[].text / picUrl / area / userNickName
      data.item.main.clickParam.args.publishTime / wantNum / tag
      exContent.userFishShopLabel.tagList[].data.content = 「N条评价 / 好评率X%」
      exContent.userIdentityShow = 平台身份徽标（如「闲鱼严选卖家」）
接口名与风控/限流标记串都在 selectors.py，本文件不写死。
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from . import selectors as sel
from .models import Listing

BASE_URL = sel.BASE_URL
SEARCH_API = sel.SEARCH_API


class ParseError(Exception):
    """接口返回结构不认识。"""


class ApiRejected(Exception):
    """接口明确拒绝（风控 / 未登录 / 参数错误）。"""

    def __init__(self, message: str, ret: str = ""):
        super().__init__(message)
        self.ret = ret


def item_url(item_id: str) -> str:
    return f"{BASE_URL}/item?{urlencode({'id': item_id})}"


def price_from_text(text: str) -> float | None:
    cleaned = str(text or "").replace(",", "").replace("¥", "").strip()
    if not cleaned:
        return None
    match = re.search(r"\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    value = float(match.group(0))
    if "万" in cleaned:
        value *= 10_000
    if not math.isfinite(value) or value <= 0:
        return None
    return round(value, 2)


def _price_text(raw: Any) -> str:
    if isinstance(raw, list):
        return "".join(str(part.get("text", "")) for part in raw if isinstance(part, dict))
    if isinstance(raw, dict):
        return str(raw.get("text") or raw.get("price") or raw.get("value") or "")
    return str(raw or "")


def _int(value: Any) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 0
    if number < 0:
        return 0
    return number


def _tags(ex_content: dict, click_args: dict) -> list[str]:
    tags: list[str] = []
    if click_args.get("tag") == "freeship":
        tags.append("包邮")
    fish_tags = ex_content.get("fishTags")
    if isinstance(fish_tags, dict):
        rank_one = fish_tags.get("r1")
        tag_list = rank_one.get("tagList", []) if isinstance(rank_one, dict) else []
    elif isinstance(fish_tags, list):
        tag_list = fish_tags
    else:
        tag_list = []
    for entry in tag_list if isinstance(tag_list, list) else []:
        content = ""
        if isinstance(entry, dict):
            data = entry.get("data")
            content = str((data.get("content") if isinstance(data, dict) else "") or entry.get("content") or "")
        else:
            content = str(entry)
        if "验货宝" in content and "验货宝" not in tags:
            tags.append("验货宝")
    return tags


# D2 卖家画像：搜索接口零额外请求带回，只标注不排除。
_REVIEW_COUNT = re.compile(r"(\d+)\s*条评价")
_RATING_PERCENT = re.compile(r"好评率\s*(\d+)\s*%")


def _label_contents(ex_content: dict) -> list[str]:
    """userFishShopLabel.tagList[].data.content 的文本列表（「N条评价」「好评率X%」）。"""
    label = ex_content.get("userFishShopLabel")
    if not isinstance(label, dict):
        return []
    tag_list = label.get("tagList")
    if not isinstance(tag_list, list):
        return []
    contents: list[str] = []
    for entry in tag_list:
        if isinstance(entry, dict):
            data = entry.get("data")
            content = (data.get("content") if isinstance(data, dict) else "") or entry.get("content")
        else:
            content = entry
        text = str(content or "").strip()
        if text:
            contents.append(text)
    return contents


def seller_signals(ex_content: dict) -> tuple[int, int, str]:
    """从 exContent 解析卖家画像：(评价数, 好评率 0-100, 平台身份徽标)。

    样本实测 30/30 都带「N条评价 / 好评率X%」；身份徽标少数有（如「闲鱼严选卖家」）。
    字段缺失时返回 0/0/空串——当作「没有信号」，不当成异常。
    """
    reviews = 0
    rating = 0
    for content in _label_contents(ex_content):
        if not reviews:
            match = _REVIEW_COUNT.search(content)
            if match:
                reviews = int(match.group(1))
        if not rating:
            match = _RATING_PERCENT.search(content)
            if match:
                rating = int(match.group(1))
    identity = str(ex_content.get("userIdentityShow") or "").strip()
    return reviews, rating, identity


def parse_item(wrapper: dict[str, Any]) -> Listing | None:
    """把 resultList 里的一项转成 Listing；不认识的条目返回 None。"""
    if not isinstance(wrapper, dict):
        return None
    data = wrapper.get("data")
    if not isinstance(data, dict):
        return None
    item = data.get("item")
    main = item.get("main") if isinstance(item, dict) else None

    if isinstance(main, dict) and isinstance(main.get("exContent"), dict):
        ex = main["exContent"]
        click_args = (main.get("clickParam") or {}).get("args") or {}
    else:
        ex = data
        click_args = (data.get("clickParam") or {}).get("args") or {}
    if not isinstance(ex, dict):
        return None
    if not isinstance(click_args, dict):
        click_args = {}

    item_id = str(ex.get("itemId") or ex.get("id") or "").strip()
    if not item_id:
        return None

    price = price_from_text(_price_text(ex.get("price")))
    if price is None:
        return None

    publish = _int(click_args.get("publishTime") or ex.get("publishTime"))
    # 有的字段是秒级
    if 0 < publish < 100_000_000_000:
        publish *= 1000

    title = str(ex.get("title") or "").strip()
    if not title:
        return None

    seller_reviews, seller_rating, seller_identity = seller_signals(ex)

    return Listing(
        item_id=item_id,
        title=title,
        price=price,
        url=item_url(item_id),
        image=str(ex.get("picUrl") or ""),
        location=str(ex.get("area") or ex.get("city") or "").strip(),
        seller=str(ex.get("userNickName") or ex.get("userNick") or "").strip(),
        seller_id=str(
            click_args.get("seller_id")
            or click_args.get("sellerId")
            or ex.get("sellerId")
            or ""
        ).strip(),
        publish_ts=publish,
        wants=_int(click_args.get("wantNum") or ex.get("wantNum")),
        tags=_tags(ex, click_args),
        seller_reviews=seller_reviews,
        seller_rating=seller_rating,
        seller_identity=seller_identity,
    )


def parse_payload(payload: dict[str, Any]) -> tuple[list[Listing], int]:
    """返回 (商品列表, 是否还有下一页的提示位)。

    风控 / 未登录会抛 ApiRejected，结构异常抛 ParseError。
    """
    if not isinstance(payload, dict):
        raise ParseError("接口返回不是 JSON 对象")

    ret = payload.get("ret")
    ret_values = ret if isinstance(ret, list) else [ret]
    ret_values = [str(value or "") for value in ret_values]
    failures = [value for value in ret_values if not value.upper().startswith("SUCCESS::")]
    if failures:
        text = "; ".join(failures)
        # 顺序按「能不能自己恢复」排：限流等一等就好 → 风控要人工过 → 登录墙。
        rate = sel.first_match(text, sel.API_RATE_LIMIT_MARKERS)
        if rate:
            raise ApiRejected(
                f"闲鱼接口限流（{rate}）：等 20-30 分钟再看，连续重试只会加重", ret=text
            )
        risk = sel.first_match(text, sel.API_RISK_MARKERS)
        if risk:
            raise ApiRejected(
                f"闲鱼风控拦截（{risk}）：请在有头窗口里手动过一下验证，不要连续重试", ret=text
            )
        if sel.first_match(text, sel.API_LOGIN_MARKERS):
            raise ApiRejected("接口要求登录态，请重新运行 login.bat 扫码", ret=text)
        raise ApiRejected(f"接口拒绝：{text}", ret=text)

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ParseError("接口缺少 data 字段")
    result_list = data.get("resultList")
    if result_list is None:
        return [], 0
    if not isinstance(result_list, list):
        raise ParseError("resultList 不是数组")

    listings: list[Listing] = []
    for wrapper in result_list:
        listing = parse_item(wrapper)
        if listing is not None:
            listings.append(listing)
    return listings, 0


def describe_ret(payload: dict[str, Any]) -> str:
    ret = payload.get("ret") or []
    return "; ".join(str(value) for value in (ret if isinstance(ret, list) else [ret]))


def format_age(publish_ts: int, now_ms: int) -> str:
    if publish_ts <= 0:
        return "时间未知"
    seconds = max(0, (now_ms - publish_ts) / 1000)
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


def format_time(publish_ts: int) -> str:
    if publish_ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(publish_ts / 1000).strftime("%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def loads(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ParseError(f"接口返回不是合法 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ParseError("接口返回不是 JSON 对象")
    return value
