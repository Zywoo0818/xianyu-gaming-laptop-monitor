"""领域模型：一条商品 + 一次筛选分析的结论。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Listing:
    item_id: str
    title: str
    price: float
    url: str
    image: str = ""
    location: str = ""
    seller: str = ""
    publish_ts: int = 0  # 毫秒时间戳，0 表示未知
    wants: int = 0
    tags: list[str] = field(default_factory=list)
    keyword: str = ""
    watch: str = ""
    desc: str = ""

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "price": self.price,
            "url": self.url,
            "image": self.image,
            "location": self.location,
            "seller": self.seller,
            "publish_ts": self.publish_ts,
            "wants": self.wants,
            "tags": list(self.tags),
            "keyword": self.keyword,
            "watch": self.watch,
            "desc": self.desc,
        }


@dataclass
class Analysis:
    score: float
    verdict: str
    ref_price: float
    ratio: float
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    spec_delta: float = 0.0
    suspicious: bool = False
    market_median: float = 0.0
    excluded: bool = False
    gpu: str = ""
    gpu_score: float = 0.0
    brand: str = ""
    brand_score: float = 0.0
    table_name: str = ""
    ref_source: str = ""
    fatal: bool = False
