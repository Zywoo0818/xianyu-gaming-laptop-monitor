"""v3 粗筛：只使用搜索页已有信息，不打开任何详情页。

规则：
- 商家模块 1：卖家昵称、商品标题/介绍里的商家词
- 可用性：无法开机等开机故障
- 品牌：华为、苹果
- 显卡：识别不到英伟达显卡、非英伟达卡、工作站专业卡都排除
- 继承的硬规则：N95、空机/缺内存硬盘、内存 <8G、硬盘 <120G
- 不再走品类闸门
"""
from __future__ import annotations

from . import brands, gpus
from .config import Config
from .models import Listing
from .pricing import fatal_reject_reason
from .textmatch import hit_words

# 搜索接口的 title 是「标题 + 描述全文」，品牌只看开头，避免描述里的对比句误伤。
BRAND_HEAD_CHARS = 80

# 非整机标题：求购、租赁、拆机、代拍不进入细筛。否定句（无拆机/不是求购）由 hit_words 放行。
COARSE_HARD_EXCLUDE = ("求购", "收一台", "租赁", "出租", "拆机", "代拍")


def coarse_reject(listing: Listing, cfg: Config) -> str:
    """返回排除原因；返回空字符串表示粗筛通过。"""
    fatal = fatal_reject_reason(listing, cfg)
    if fatal:
        return fatal
    text = f"{listing.title}\n{listing.desc or ''}"
    hard = hit_words(text, COARSE_HARD_EXCLUDE)
    if hard:
        return f"非整机：命中「{hard[0]}」"
    brand = brands.detect(listing.title[:BRAND_HEAD_CHARS], listing.seller)
    if brand.excluded:
        return f"{brand.name}品牌，按要求排除"
    gpu = gpus.detect(listing.title)
    if gpu is None:
        return "识别不到英伟达显卡，排除"
    if gpu.is_workstation:
        return "工作站/专业卡，不是游戏本"
    if not gpu.is_nvidia:
        return "非英伟达显卡，排除"
    return ""
