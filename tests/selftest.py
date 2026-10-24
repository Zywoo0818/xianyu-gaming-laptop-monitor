"""方案二离线自测：解析 / 接口样本 / 集中选择器 / 粗筛 / 登录态 / 排序提前停 / 熔断 / 报表 / loop。

全部不联网：接口结构靠 tests/fixtures 的 JSON 样本，抓取靠假 Crawler / 假页面对象。
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import re
import shutil
import sys
import threading
import time
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from xianyu import __version__  # noqa: E402
from unittest.mock import patch  # noqa: E402

from xianyu.cli import (  # noqa: E402
    BANNER,
    _LoopController,
    _console_command,
    _mark_scan_items,
    _open_latest_report,
    _push_if_configured,
    _scan_db_path,
    build_parser,
    cmd_mark,
    cmd_report,
)
from xianyu.budget import DailyBudget, budget_path  # noqa: E402
from xianyu import selectors as sel  # noqa: E402
from xianyu import gpus  # noqa: E402
from xianyu.coarse import coarse_reject  # noqa: E402
from xianyu.config import Config, NotifyConfig, Watch  # noqa: E402
from xianyu.crawler import (  # noqa: E402
    CaptureTimeout,
    Crawler,
    CrawlError,
    LoginRequired,
    PaginationError,
    RateLimited,
    RiskControl,
    build_search_url,
    is_logged_in,
    login_cookie_names,
    login_state_path,
    looks_newest_first,
    should_stop_early,
)
from xianyu.demo import load_saved_listings  # noqa: E402
from xianyu.engine import (  # noqa: E402
    FRESHNESS_REASON,
    ScanPaused,
    _RoundData,
    _coarse_filter,
    _finalize,
    _search_all,
    classify_round,
    run_round,
)
from xianyu.fine import cache_fresh, classify_item, evaluate_page  # noqa: E402
from xianyu.guard import (  # noqa: E402
    RiskGuard,
    classify_errors,
    error_kind,
    is_login_error,
    is_rate_limit,
    is_risk_error,
)
from xianyu.models import Analysis, Listing, SellerItem, SellerPage, SellerRef  # noqa: E402
from xianyu.notify import NotifyResult, _check_response, push  # noqa: E402
from xianyu.parse import ApiRejected, ParseError, parse_item, parse_payload, seller_signals  # noqa: E402
from xianyu.pricetable import PriceRow, match_reference, normalize_name  # noqa: E402
from xianyu.pricing import analyze, fatal_reject_reason, gate, is_fresh  # noqa: E402
from xianyu.report import (  # noqa: E402
    ignored_row_to_reject_item,
    is_pro_seller,
    rejected_item,
    reject_row_to_item,
    render_html,
    render_markdown,
    row_to_item,
    seller_signal_note,
    write_all,
    write_csv,
    write_reject_csv,
)
from xianyu.rescreen import apply_rescreen, plan_rescreen, rescreen_reason  # noqa: E402
from xianyu.store import (  # noqa: E402
    MANUAL_REJECT_REASON,
    RESCREEN_NOTE_PREFIX,
    connect,
    finish_scan,
    ignore_pending,
    latest_scan,
    load_seller_checks,
    mark_manual_reject,
    mark_notified,
    restore_pending,
    rows_ignored,
    rows_pending,
    rows_rejects,
    rows_manual_rejects,
    save_rejects,
    save_seller_checks,
    set_status,
    start_scan,
    upsert,
)

PASSED = 0
TMP = ROOT / "data" / "_selftest_v3"
CFG = Config.load(ROOT / "config.yaml")

FIXTURE_ROWS = [
    PriceRow(name="拯救者R9000P", gpu="3060", price=3650, norm=normalize_name("拯救者R9000P")),
    PriceRow(name="拯救者R7000P", gpu="3060", price=3400, norm=normalize_name("拯救者R7000P")),
    PriceRow(name="天选4", gpu="4060", price=5000, norm=normalize_name("天选4")),
]


def check(condition: bool, message: str) -> None:
    global PASSED
    if not condition:
        raise AssertionError(message)
    PASSED += 1
    print(f"  ✓ {message}")


def sample_wrapper(
    item_id: str = "1001",
    title: str = "联想拯救者R9000P 2021 RTX3060 16G",
    price: str = "3300",
    seller_id: str = "seller-1",
) -> dict:
    return {
        "data": {
            "item": {
                "main": {
                    "exContent": {
                        "itemId": item_id,
                        "title": title,
                        "price": [{"text": price}],
                        "area": "上海",
                        "userNickName": "个人卖家",
                        "picUrl": "",
                    },
                    "clickParam": {
                        "args": {
                            "publishTime": str(int(time.time() * 1000)),
                            "wantNum": "12",
                            "seller_id": seller_id,
                        }
                    },
                }
            }
        }
    }


def make_listing(
    title: str,
    *,
    item_id: str = "c1",
    price: float = 3500,
    seller: str = "",
    desc: str = "",
) -> Listing:
    return Listing(item_id, title, price, "u", seller=seller, desc=desc)


def neutral_analysis() -> Analysis:
    return Analysis(
        score=0.0,
        verdict="待审核",
        ref_price=0.0,
        ratio=1.0,
        reasons=["粗筛 + 细筛通过"],
        flags=[],
    )


def make_page(*titles: str, seller_id: str = "s1", in_sale: int = 3) -> SellerPage:
    return SellerPage(
        seller_id=seller_id,
        seller_name="卖家",
        url="https://www.goofish.com/personal?userId=x",
        in_sale=in_sale,
        items=[
            SellerItem(item_id=f"{seller_id}-{index}", title=title)
            for index, title in enumerate(titles, start=1)
        ],
    )


# ---------------------------------------------------------------------------- parse
def test_parse() -> None:
    print("parse")
    special_id = "u1puyGhmFvz4Wbw/sD6bKw=="
    listing = parse_item(sample_wrapper(seller_id=special_id))
    check(listing is not None, "解析出商品")
    assert listing
    check(listing.price == 3300, "价格解析正确")
    check(listing.url == "https://www.goofish.com/item?id=1001", "链接由 ID 生成")
    check(listing.seller_id == special_id, "卖家 ID 原样保留（含 / 和 =）")
    check(listing.wants == 12 and listing.location == "上海", "想要人数和地区解析正确")

    items, more = parse_payload(
        {
            "ret": ["SUCCESS::调用成功"],
            "data": {"resultList": [sample_wrapper("2", "B", "99"), {"bad": 1}]},
        }
    )
    check(len(items) == 1 and more == 0, "跳过无法识别的条目")

    check(
        parse_item({"data": {"item": {"main": {"exContent": {"itemId": "x"}}}}}) is None,
        "缺价格/标题的条目返回 None",
    )

    try:
        parse_payload({"ret": ["FAIL_SYS_USER_VALIDATE::RGV587"], "data": {}})
    except ApiRejected as exc:
        check("风控" in str(exc), "RGV587 被识别为风控")
    else:
        check(False, "RGV587 应该抛 ApiRejected")

    # D2 卖家画像：零额外请求，来自 exContent 的鱼小铺标签和身份徽标
    wrapper = sample_wrapper("2001", "联想拯救者R9000P RTX3060 16G", "3300")
    ex = wrapper["data"]["item"]["main"]["exContent"]
    ex["userFishShopLabel"] = {
        "tagList": [
            {"data": {"content": "1538条评价"}},
            {"data": {"content": "好评率65%"}},
        ]
    }
    ex["userIdentityShow"] = "闲鱼严选卖家"
    listing = parse_item(wrapper)
    assert listing
    check(
        (listing.seller_reviews, listing.seller_rating, listing.seller_identity)
        == (1538, 65, "闲鱼严选卖家"),
        "D2：解析评价数 / 好评率 / 身份徽标",
    )
    plain = parse_item(sample_wrapper("2002", "联想拯救者R9000P RTX3060 16G", "3300"))
    assert plain
    check(
        (plain.seller_reviews, plain.seller_rating, plain.seller_identity) == (0, 0, ""),
        "D2：没有卖家画像字段时按无信号处理",
    )
    weird = sample_wrapper("2003", "联想拯救者R9000P RTX3060 16G", "3300")
    weird_ex = weird["data"]["item"]["main"]["exContent"]
    weird_ex["userFishShopLabel"] = {"tagList": ["垃圾数据", {"data": None}, 42]}
    weird_ex["userIdentityShow"] = None
    weird_listing = parse_item(weird)
    assert weird_listing
    check(
        (weird_listing.seller_reviews, weird_listing.seller_rating, weird_listing.seller_identity)
        == (0, 0, ""),
        "D2：画像字段结构异常时不炸，按无信号处理",
    )
    check(seller_signals({}) == (0, 0, ""), "D2：空 exContent 返回无信号")


# --------------------------------------------------------------------------- coarse
def test_coarse() -> None:
    print("coarse")
    check(
        coarse_reject(
            make_listing("联想拯救者R9000P RTX3060 16G 512G 游戏本 个人自用", seller="小陈同学"),
            CFG,
        )
        == "",
        "普通个人游戏本粗筛通过",
    )
    check(
        "识别不到英伟达显卡" in coarse_reject(make_listing("游戏本 i7 16G 512G 高配 个人闲置"), CFG),
        "识别不到 N 卡的先排除",
    )
    check(
        "识别不到英伟达显卡" in coarse_reject(make_listing("联想小新 Air14 轻薄本 办公本 i5 16G 512G"), CFG),
        "轻薄本没有 N 卡，粗筛排除",
    )
    check(
        "非英伟达显卡" in coarse_reject(make_listing("拯救者R9000P RX6600 16G 512G 游戏本"), CFG),
        "A 卡排除",
    )
    check(
        "工作站" in coarse_reject(make_listing("ThinkPad P15 RTX A4000 32G 1T"), CFG),
        "工作站专业卡排除",
    )
    check(gpus.detect("台式机 GTX1080 8G 显卡").model == "1080", "GTX1080 仍识别为显卡")
    check(gpus.detect("15.6寸大屏幕1080高清屏") is None, "1080高清屏不当显卡")
    check(gpus.detect("1920x1080 屏幕") is None, "1920x1080 不当显卡")
    check(
        "识别不到英伟达显卡"
        in coarse_reject(make_listing("华硕飞行堡垒 i7 12G 128G 16寸大屏幕1080高清屏幕"), CFG),
        "只有 1080 屏描述的整机不因分辨率被当成 N 卡",
    )

    check("苹果" in coarse_reject(make_listing("MacBook Pro 14 M1 Pro 16G 512G"), CFG), "苹果排除")
    check("华为" in coarse_reject(make_listing("华为 MateBook 14 16G 512G"), CFG), "华为排除")

    long_title = "游戏本 RTX3060 个人自用" * 12 + " 描述里提到 MacBook 对比"
    check(
        coarse_reject(make_listing(long_title), CFG) == "",
        "品牌只检查标题前 80 字，描述里的 MacBook 不误杀",
    )

    check(
        "疑似商家" in coarse_reject(make_listing("华强北数码城 出 拯救者R9000P RTX3060 16G"), CFG),
        "标题里的商家词被硬排除",
    )
    check(
        "疑似商家" in coarse_reject(make_listing("拯救者R9000P RTX3060 16G", desc="本店实体店经营"), CFG),
        "商品介绍里的商家词被硬排除",
    )
    check(
        "疑似商家" in coarse_reject(make_listing("拯救者R9000P RTX3060 16G", seller="华强北数码城"), CFG),
        "卖家昵称里的 seller_exclude 词被硬排除",
    )
    for seller in (
        "圆满小店",
        "星辰精选",
        "小白精品二手笔记本",
        "笔记本专营",
        "神舟战神小卖部",
        "购物天堂",
        "鼎丰数码优品",
    ):
        check(
            "疑似商家" in coarse_reject(make_listing("拯救者R9000P RTX3060 16G", seller=seller), CFG),
            f"卖家昵称命中排除词：{seller}",
        )
    check(
        coarse_reject(make_listing("拯救者R9000P RTX3060 16G", seller="小白同学"), CFG) == "",
        "普通个人昵称不带「笔记本」时不误杀",
    )
    check(
        coarse_reject(
            make_listing("拯救者R9000P RTX3060 16G 精选个人自用", seller="小陈同学"), CFG
        )
        == "",
        "「精选」只在卖家昵称里排除，标题正常描述不误杀",
    )

    check("空机" in coarse_reject(make_listing("拯救者R9000P RTX3060 空机出售"), CFG), "空机排除")
    check("缺少内存" in coarse_reject(make_listing("拯救者R9000P RTX3060 不含内存"), CFG), "不含内存排除")
    check("缺少内存" in coarse_reject(make_listing("拯救者R9000P RTX3060 无硬盘"), CFG), "无硬盘排除")
    check(
        "缺少内存" in coarse_reject(make_listing("拯救者R9000P RTX3060 内存硬盘都没有"), CFG),
        "内存硬盘都没有排除",
    )
    check(
        "低配（内存 4G）" in coarse_reject(make_listing("拯救者R9000P RTX3060 4G内存 512G固态"), CFG),
        "内存小于 8G 排除",
    )
    check(
        "低配（硬盘 64G）" in coarse_reject(make_listing("拯救者R9000P RTX3060 16G内存 64G固态"), CFG),
        "硬盘小于 120G 排除",
    )
    check("开机故障" in coarse_reject(make_listing("拯救者R9000P RTX3060 无法开机"), CFG), "无法开机排除")
    check("N95" in coarse_reject(make_listing("拯救者R9000P RTX3060 16G 512G 处理器 N95"), CFG), "N95 排除")

    check(coarse_reject(make_listing("神舟 N950KP RTX3060 游戏本"), CFG) == "", "N950KP 不误伤")
    check(
        coarse_reject(make_listing("拯救者R9000P RTX3060 16G 512G 送 N95 口罩"), CFG) == "",
        "N95 口罩不误伤",
    )
    check(
        coarse_reject(make_listing("拯救者R9000P RTX3060 16G 无硬盘位"), CFG) == "",
        "「无硬盘位」描述扩展能力，不当作缺硬盘",
    )

    for title in (
        "求购 联想拯救者R9000P RTX3060 16G",
        "收一台 游戏本 RTX3060 16G",
        "游戏本 租赁 月付 RTX3060",
        "出租 拯救者R9000P RTX3060 16G",
        "拆机 拯救者R9000P RTX3060 16G",
        "代拍 游戏本 RTX3060 16G",
    ):
        check("非整机" in coarse_reject(make_listing(title), CFG), f"非整机标题排除：{title[:20]}")
    check(
        coarse_reject(make_listing("拯救者R9000P RTX3060 16G 无拆机 个人自用"), CFG) == "",
        "「无拆机」按否定句放行",
    )
    check(
        coarse_reject(make_listing("拯救者R9000P RTX3060 16G 不是求购 个人自用"), CFG) == "",
        "「不是求购」按否定句放行",
    )
    check(
        coarse_reject(make_listing("拯救者R9000P RTX3060 16G 非租赁 个人自用"), CFG) == "",
        "「非租赁」按否定句放行",
    )


# ----------------------------------------------------------------------------- fine
def test_fine() -> None:
    print("fine")
    check(classify_item("联想拯救者R9000P RTX3060 16G 游戏本", CFG) == "laptop", "笔记本识别")
    check(classify_item("神舟战神Z8 游戏本", CFG) == "laptop", "系列名识别笔记本")
    check(classify_item("15.6寸 屏幕 1080P", CFG) == "laptop", "尺寸描述识别笔记本")
    check(classify_item("金士顿 8G DDR4 内存条", CFG) == "part", "内存条识别为配件")
    check(classify_item("三星 512G NVMe 固态硬盘", CFG) == "part", "固态硬盘识别为配件")
    check(classify_item("iPhone 15 Pro", CFG) == "other", "无关商品归为其他")

    normal = evaluate_page(make_page("联想拯救者R9000P RTX3060 16G 游戏本"), CFG)
    check(normal.ok and normal.laptop_count == 1, "卖家主页只有一台游戏本：通过")
    check(normal.reason.startswith("细筛通过"), "通过理由说明主页情况")

    dji = evaluate_page(
        make_page("华硕天选4 RTX4060 游戏本", "大疆 Mini 3 Pro 无人机 畅飞套装"), CFG
    )
    check(dji.verdict == "dji" and dji.matched_item == "s1-2", "卖家其他商品含大疆：直接排除")

    for title in ("DJI Mini 3 Pro", "dji Air 3", "D J I Mavic 3", "d  j  i  Neo"):
        result = evaluate_page(make_page("华硕天选4 RTX4060 游戏本", title), CFG)
        check(result.verdict == "dji", f"DJI 正则忽略大小写/空格：{title}")
    check(
        evaluate_page(make_page("华硕天选4 RTX4060 游戏本", "大量现货 游戏本"), CFG).ok,
        "「大量」不会误判成「大疆」",
    )
    check(
        evaluate_page(make_page("华硕天选4 RTX4060 游戏本", "ADJIMA 电脑配件"), CFG).ok,
        "DJI 两侧紧贴字母时不误判",
    )

    parts = [
        "金士顿 8G DDR4 内存条",
        "三星 512G NVMe 固态硬盘",
        "RTX3060 显卡 拆机",
        "屏幕 1080P 总成",
        "230W 电源适配器 原装",
    ]
    merchant = evaluate_page(make_page(*parts), CFG)
    check(merchant.verdict == "merchant" and merchant.part_count == 5, "配件 ≥5 条：按商家排除")

    mixed = evaluate_page(make_page(*parts, "大疆 Mini 3"), CFG)
    check(mixed.verdict == "dji", "大疆优先于商家阈值判断")

    four = evaluate_page(make_page(*parts[:4]), CFG)
    check(four.ok and four.part_count == 4, "配件 4 条：仍在阈值以下，通过")

    small_cfg = replace(CFG, fine=replace(CFG.fine, item_limit=3))
    limited = evaluate_page(make_page(*parts, "联想拯救者R9000P 游戏本"), small_cfg)
    check(limited.ok and limited.item_count == 3, "只统计首屏 item_limit 条，不滚动加载")

    now = int(time.time())
    check(cache_fresh(now - 10, 24, now), "24 小时内缓存有效")
    check(not cache_fresh(now - 90000, 24, now), "超过 24 小时缓存失效")
    check(not cache_fresh(now, 0, now), "cache_hours=0 时不使用缓存")
    check(not cache_fresh(0, 24, now), "没有检查时间时不使用缓存")


# ------------------------------------------------------------------- crawler edges
def test_crawler_edges() -> None:
    print("crawler edges")
    watch = CFG.watches[0]
    messages: list[str] = []

    class FailingCrawler:
        stats = {"search_requests": 0}

        def search(self, *args, **kwargs):
            raise PaginationError("第 2 页翻页失败或超时，本轮结束")

    data = _RoundData()
    _search_all(CFG, FailingCrawler(), [watch], data, 2, None, messages.append)
    check(data.aborted, "翻页失败立即结束本轮")
    check("第 2 页" in data.errors.get(watch.name, ""), "翻页失败写入本轮错误")
    clean, groups = classify_round(
        [watch], {"游戏本": [make_listing("游戏本 RTX3060")]}, data.errors
    )
    check(not clean and groups["soft"], "翻页失败不再算干净轮")

    class PartialCrawler:
        stats = {"search_requests": 2}
        sort_status: dict[str, str] = {}
        last_search_error = "第 2 页翻页失败或超时，已保留前 20 条"

        def __init__(self):
            self.calls = 0

        def search(self, *args, **kwargs):
            self.calls += 1
            return [make_listing("游戏本 RTX3060 16G 512G")]

    partial = PartialCrawler()
    second = Watch(name="换关键词", keyword="游戏本2", pages=2)
    data = _RoundData()
    _search_all(CFG, partial, [watch, second], data, 2, None, messages.append)
    check(
        len(data.collected.get(watch.name, [])) == 1,
        "翻页失败保留已抓到的页，不整轮丢弃",
    )
    check("已保留前 20 条" in data.errors.get(watch.name, ""), "翻页失败原因记进本轮错误")
    check(partial.calls == 1 and not data.aborted, "翻页失败后不再发后续关键词，但不当作中止")
    clean, groups = classify_round(
        [watch], data.collected, data.errors
    )
    check(not clean and groups["soft"], "翻页软失败仍不算干净轮")

    class LoginCrawler:
        stats = {"search_requests": 0}

        def search(self, *args, **kwargs):
            raise LoginRequired("未登录或登录态已失效：请先运行 login.bat 扫码登录")

    data = _RoundData()
    _search_all(CFG, LoginCrawler(), [watch], data, 2, None, messages.append)
    check(data.aborted, "登录失效立即结束本轮")
    check(
        "需要登录" in data.errors.get(watch.name, ""),
        "登录失效写入本轮错误并提示重新扫码",
    )

    crawler = Crawler(CFG)
    check(crawler.stats == {"search_requests": 0}, "方案二 crawler 只统计搜索请求")
    check(crawler.headless is False, "方案二默认有头运行")

    from types import SimpleNamespace

    crawler._on_request(
        SimpleNamespace(url="https://h5api.m.goofish.com" + sel.SEARCH_API_PATH + "/")
    )
    crawler._on_request(SimpleNamespace(url="https://www.goofish.com/search?q=x"))
    check(crawler.stats["search_requests"] == 1, "请求监听按发出的搜索接口计数，忽略页面请求")
    crawler._on_response(
        SimpleNamespace(
            url="https://h5api.m.goofish.com" + sel.SEARCH_API_PATH + "/",
            request=SimpleNamespace(),
            body=lambda: (_ for _ in ()).throw(RuntimeError("body gone")),
        )
    )
    check(crawler.stats["search_requests"] == 1, "响应解析失败不影响已发出的请求计数")

    clicks: list[str] = []
    crawler._click_arrow_js = lambda page, number: clicks.append("JS") or True
    crawler._click_page_number = lambda page, number: clicks.append("页码") or True
    crawler._click_arrow = lambda page, number: clicks.append("箭头") or True
    crawler._wait_capture = lambda *args, **kwargs: (_ for _ in ()).throw(
        CaptureTimeout("no request")
    )
    check(crawler._advance(object(), 2) is None, "翻页无响应时返回结束信号")
    check(clicks == ["JS"], "翻页只点一次：超时后不再换策略补点")

    clicks.clear()
    crawler._wait_capture = lambda *args, **kwargs: {"page": 2}
    check(crawler._advance(object(), 2) == {"page": 2}, "翻页成功后返回新一页结果")
    check(clicks == ["JS"], "翻页成功也只点一次")

    clicks.clear()

    def click_then_fail(page, number):
        clicks.append("JS")
        raise RuntimeError("execution context destroyed after click")

    crawler._click_arrow_js = click_then_fail
    check(crawler._advance(object(), 2) is None, "翻页动作异常时结束本轮")
    check(clicks == ["JS"], "异常可能发生在点击之后：不再换策略补点")


# ---------------------------------------------------------------------------- store
def test_store() -> None:
    print("store")
    conn = connect(TMP / "store.db")
    try:
        scan_id = start_scan(conn, 1)
        listing = Listing(
            "item-1",
            "联想拯救者R9000P RTX3060 16G 512G 游戏本",
            3300,
            "https://www.goofish.com/item?id=item-1",
            seller="小陈同学",
            seller_id="u1puyGhmFvz4Wbw/sD6bKw==",
        )
        analysis = neutral_analysis()
        check(upsert(conn, listing, analysis, scan_id) is True, "第一次写入是新商品")
        check(upsert(conn, listing, analysis, scan_id) is False, "重复写入不是新商品")

        row = conn.execute("SELECT * FROM listings WHERE item_id='item-1'").fetchone()
        check(row["seller_id"] == listing.seller_id, "卖家 ID 落库")
        check(int(row["first_scan_id"]) == scan_id, "首次扫描编号落库")
        next_scan = start_scan(conn, 1)
        upsert(conn, listing, analysis, next_scan)
        row = conn.execute("SELECT * FROM listings WHERE item_id='item-1'").fetchone()
        check(int(row["first_scan_id"]) == scan_id, "重复出现不改首次扫描编号")
        check(len(rows_pending(conn)) == 1, "待审核查询能取到商品")

        finish_scan(
            conn,
            next_scan,
            pages=2,
            fetched=9,
            kept=7,
            sort_status="游戏本：已点「新发布 → 最新」",
        )
        scan_row = conn.execute("SELECT * FROM scans WHERE id=?", (next_scan,)).fetchone()
        check(scan_row["fetched"] == 9 and scan_row["kept"] == 7, "扫描记录写入统计")
        check(
            scan_row["sort_status"] == "游戏本：已点「新发布 → 最新」",
            "扫描记录保存每个关键词的排序状态",
        )

        unfinished = start_scan(conn, 1)
        check(latest_scan(conn)["id"] == next_scan, "未收尾的轮次不算最新扫描")

        set_status(conn, "item-1", "adopted")
        check(rows_pending(conn) == [], "已采纳商品不进入待审核")

        for item_id in ("a", "b"):
            upsert(
                conn,
                Listing(item_id, "联想拯救者R9000P RTX3060 16G 512G 游戏本", 3300, "u"),
                analysis,
                scan_id,
            )
        set_status(conn, "b", "adopted")
        changed = ignore_pending(conn, {"a": "空机", "b": "缺内存"})
        check(changed == 1, "自动忽略只改仍在待审核的条目")
        statuses = {
            row["item_id"]: row["status"]
            for row in conn.execute("SELECT item_id, status FROM listings")
        }
        check(statuses["a"] == "ignored" and statuses["b"] == "adopted", "人工状态不被自动忽略覆盖")

        upsert(
            conn,
            Listing(
                "manual",
                "华硕天选2 RTX3050 16G 512G 游戏本",
                3600,
                "https://www.goofish.com/item?id=manual",
                seller="个人卖家",
                seller_id="seller-manual",
            ),
            analysis,
            next_scan,
        )
        check(mark_manual_reject(conn, "manual"), "人工忽略写入成功")
        reject_scan = conn.execute(
            "SELECT scan_id FROM scan_rejects WHERE item_id='manual'"
        ).fetchone()
        check(
            reject_scan is not None and int(reject_scan["scan_id"]) == next_scan,
            "人工忽略写进最近完成的轮次，而不是未收尾轮次",
        )
        manual_row = conn.execute(
            "SELECT * FROM listings WHERE item_id='manual'"
        ).fetchone()
        check(
            manual_row["status"] == "ignored" and manual_row["note"] == MANUAL_REJECT_REASON,
            "人工忽略状态与备注落库",
        )
        rejects = rows_rejects(conn, next_scan)
        manual_reject = [row for row in rejects if row["item_id"] == "manual"]
        check(
            len(manual_reject) == 1
            and manual_reject[0]["reason"] == MANUAL_REJECT_REASON
            and manual_reject[0]["seller"] == "个人卖家",
            "人工忽略写进最新一轮不符合明细",
        )
        ignored_ids = {row["item_id"] for row in rows_ignored(conn)}
        check("a" in ignored_ids and "manual" not in ignored_ids, "人工不符合不在自动忽略里重复展示")
        check("manual" not in {row["item_id"] for row in rows_pending(conn)}, "人工忽略后不再待审核")
        check(restore_pending(conn, "manual"), "人工不符合可以恢复待审核")
        manual_row = conn.execute(
            "SELECT * FROM listings WHERE item_id='manual'"
        ).fetchone()
        check(manual_row["status"] == "pending" and manual_row["note"] == "", "恢复后状态与备注清空")
        check(
            all(row["item_id"] != "manual" for row in rows_rejects(conn, next_scan)),
            "恢复后删掉人工不符合记录",
        )

        stamp = int(time.time())
        saved = save_seller_checks(
            conn,
            [
                {
                    "seller_id": "s1",
                    "seller_name": "卖家",
                    "verdict": "pass",
                    "reason": "细筛通过：主页 1 条",
                    "item_count": 1,
                    "laptop_count": 1,
                    "part_count": 0,
                    "in_sale": 3,
                    "checked_ts": stamp,
                    "checked_at": "2026-10-21 10:00:00",
                }
            ],
        )
        check(saved == 1, "卖家主页检查结果写入缓存")
        checks = load_seller_checks(conn, {"s1"})
        check(
            checks["s1"]["verdict"] == "pass" and int(checks["s1"]["laptop_count"]) == 1,
            "按卖家 ID 读回缓存",
        )
        check(load_seller_checks(conn, {"missing"}) == {}, "没有缓存的卖家返回空")
    finally:
        conn.close()


def test_scan_labeling() -> None:
    print("scan labeling")
    items = [
        {"item_id": "new", "first_scan_id": 7},
        {"item_id": "old", "first_scan_id": 3},
        {"item_id": "legacy", "first_scan_id": None},
    ]
    _mark_scan_items(items, 7)
    check(items[0]["is_new"] is True, "首次扫描编号等于本轮时标 NEW")
    check(items[1]["is_new"] is False, "更早出现的不是本轮新增")
    check(items[2]["is_new"] is False, "老库没有首次扫描编号按非新增处理")


# ---------------------------------------------------------------------------- guard
def test_guard() -> None:
    print("guard")
    root = TMP / "guard"
    cfg = replace(
        CFG,
        root=root,
        scan=replace(
            CFG.scan,
            failure_threshold=2,
            fine_failure_threshold=2,
            soft_failure_threshold=2,
            cooldown_minutes=60,
            cooldown_max_minutes=240,
            soft_cooldown_minutes=10,
            soft_cooldown_max_minutes=30,
        ),
    )
    guard = RiskGuard(cfg)
    messages: list[str] = []

    check(
        is_risk_error("风控：触发人机验证") and not is_risk_error("等待第 1 页搜索结果超时"),
        "只把风控类错误算进熔断",
    )
    groups = classify_errors(
        {
            "游戏本": "风控：人机验证",
            "细筛": "风控：卖家主页要求验证",
            "游戏本超时": "等待第 1 页搜索结果超时",
        }
    )
    check(groups["search_risk"] == ["风控：人机验证"], "搜索风控单独归类")
    check(groups["fine_risk"] == ["风控：卖家主页要求验证"], "细筛风控单独归类")
    check(groups["soft"] == ["等待第 1 页搜索结果超时"], "超时归入软失败")

    check(guard.should_skip() == (False, ""), "初始状态不拦截")
    guard.observe(clean=False, search_errors=["风控：第一次"], logger=messages.append)
    state = guard.status()
    check(state["search_failures"] == 1, "第一次搜索风控只计数不熔断")

    tripped = guard.observe(clean=False, search_errors=["风控：第二次"], logger=messages.append)
    check(tripped, "连续两次搜索风控触发熔断")
    skip, message = guard.should_skip()
    check(skip and "第 1 次熔断" in message and "--force" in message, "熔断提示等级和 --force")
    check("重新登录" not in message, "v3 熔断提示不再提重新登录")
    state = guard.status()
    check(abs(state["paused_until"] - time.time() - 3600) < 15, "首次熔断冷却 60 分钟（测试配置）")

    raw = json.loads(guard.path.read_text(encoding="utf-8"))
    raw["paused_until"] = time.time() - 1
    guard.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    state = guard.status()
    check(
        state["search_failures"] == 0 and state["paused_until"] == 0 and state["trip_level"] == 1,
        "冷却到期清空连续计数但保留熔断等级",
    )
    guard.observe(clean=False, search_errors=["风控：第三次"], logger=messages.append)
    guard.observe(clean=False, search_errors=["风控：第四次"], logger=messages.append)
    state = guard.status()
    check(
        state["trip_level"] == 2 and state["paused_until"] - time.time() > 7000,
        "再次触发升级到第 2 级冷却（120 分钟）",
    )

    guard.observe(clean=True, logger=messages.append)
    state = guard.status()
    check(
        state["trip_level"] == 0
        and state["search_failures"] == 0
        and state["fine_failures"] == 0
        and state["soft_failures"] == 0,
        "一整轮完全干净时全部归零",
    )

    # ---- 频率限制走独立短梯子，不占用按天冷却的风控档位
    mixed = classify_errors(
        {
            "游戏本": "频率限制：闲鱼接口限流（TRAFFIC）：等 20-30 分钟再看",
            "鼠标": "风控：页面出现「安全验证」",
        }
    )
    check(
        len(mixed["rate_limit"]) == 1 and len(mixed["search_risk"]) == 1 and not mixed["soft"],
        "限流与风控分开归类，都不落进软失败",
    )
    check(
        is_rate_limit("闲鱼接口限流（TRAFFIC）") and not is_risk_error("闲鱼接口限流（TRAFFIC）"),
        "限流消息不再被当成风控",
    )
    check(
        is_risk_error("风控：触发人机验证") and not is_rate_limit("风控：触发人机验证"),
        "人机验证仍是风控",
    )

    tripped = guard.observe(
        clean=False, rate_limit_errors=["频率限制：闲鱼接口限流（TRAFFIC）"], logger=messages.append
    )
    check(tripped, "限流命中即冷却（rate_limit_threshold 默认 1）")
    state = guard.status()
    check(
        state["rate_limit_failures"] == 1 and state["rate_level"] == 1 and state["trip_level"] == 0,
        "限流只升自己的等级，不污染按天风控梯子",
    )
    remaining = state["paused_until"] - time.time()
    check(1500 < remaining < 1900, "限流首次冷却按默认 30 分钟")
    skip, message = guard.should_skip()
    check(skip and "频率限制" in message and "第 1 次熔断" in message, "限流冷却提示写明原因和等级")
    guard.observe(clean=True, logger=messages.append)

    # ---- 软失败（超时/改版）也走独立等级
    guard.observe(clean=False, soft_errors=["等待第 1 页搜索结果超时"], logger=messages.append)
    check(guard.status()["soft_failures"] == 1, "单轮软失败只计数")
    guard.observe(
        clean=False,
        soft_errors=["接口结构异常，可能改版了：接口缺少 data 字段"],
        logger=messages.append,
    )
    state = guard.status()
    check(
        state["soft_level"] == 1 and state["trip_level"] == 0 and state["rate_level"] == 0,
        "软失败升自己的等级",
    )
    check(
        abs(state["paused_until"] - time.time() - 600) < 15,
        "软失败冷却按测试配置 10 分钟，不会跳到按天",
    )
    guard.observe(clean=True, logger=messages.append)
    state = guard.status()
    check(
        state["trip_level"] == 0
        and state["rate_level"] == 0
        and state["soft_level"] == 0
        and state["rate_limit_failures"] == 0
        and state["soft_failures"] == 0,
        "干净一轮把各档等级和计数一起清零",
    )

    check(
        is_login_error("需要登录：未检测到登录") and error_kind("需要登录：请重新扫码") == "login",
        "登录失效单独归类",
    )
    login_groups = classify_errors({"游戏本": "需要登录：请先运行 login.bat"})
    check(not any(login_groups.values()), "登录失效不推进任何熔断计数")

    guard.path.write_text('{"detail_failures": 2}', encoding="utf-8")
    state = guard.status()
    check(state["fine_failures"] == 2, "旧 detail_failures 状态文件可迁移为 fine_failures")

    off_raw = guard._defaults()
    off_raw["paused_until"] = time.time() + 600
    off_raw["trip_level"] = 1
    guard.path.write_text(json.dumps(off_raw, ensure_ascii=False), encoding="utf-8")
    off = RiskGuard(
        replace(
            cfg,
            scan=replace(
                cfg.scan,
                failure_threshold=0,
                fine_failure_threshold=0,
                soft_failure_threshold=0,
                rate_limit_threshold=0,
            ),
        )
    )
    check(off.should_skip() == (False, ""), "四档阈值全为 0 时关闭熔断拦截")

    guard.reset()
    raw = guard._defaults()
    raw["paused_until"] = time.time() + 7200
    raw["paused_at"] = time.time()
    raw["trip_reason"] = "搜索风控"
    guard.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    guard.observe(clean=False, search_errors=["风控：强跑后 1"], logger=messages.append)
    guard.observe(clean=False, search_errors=["风控：强跑后 2"], logger=messages.append)
    state = guard.status()
    check(
        state["paused_until"] - time.time() > 7000,
        "冷却期内再次熔断只延长不缩短原冷却",
    )


# ------------------------------------------------------------------- selectors
FORBIDDEN_LITERALS = (
    "search-pagination",
    "closeIconBg",
    "passport.goofish.com",
    "https://www.goofish.com",
    "mtop.taobao",
    "RGV587",
    "FAIL_SYS_",
)


def test_scan_lock() -> None:
    print("scan lock")
    from xianyu.lock import ScanBusy, ScanLock

    root = TMP / "lock"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "scan.lock"

    def holder_text() -> str:
        # 第 0 字节被系统锁占住，持有者信息从第 1 字节读。
        with path.open("rb") as handle:
            handle.seek(1)
            return handle.read().decode("utf-8", "replace").strip()

    with ScanLock(path):
        check("pid=" in holder_text(), "持锁时把持有者写入锁文件")
        busy = ""
        try:
            with ScanLock(path):
                pass
        except ScanBusy as exc:
            busy = str(exc)
        check("pid=" in busy, "同一把锁第二次进入被拒绝并报出持有者")

    # 进程崩溃后系统锁自动释放，文件里残留的 pid 只是文字，不会拦住下次加锁。
    path.write_text(" pid=999999 started=2000-01-01 00:00:00\n", encoding="utf-8")
    with ScanLock(path):
        check("pid=999999" not in holder_text(), "残留锁文件不阻塞，写回当前持有者")


def test_selectors_centralised() -> None:
    """改版敏感串只准出现在 selectors.py：别处写死就说明有人在偷改而不集中。"""
    print("selectors")
    for name in ("crawler.py", "parse.py", "engine.py", "report.py", "guard.py", "cli.py", "store.py"):
        text = (ROOT / "xianyu" / name).read_text(encoding="utf-8")
        leaked = [token for token in FORBIDDEN_LITERALS if token in text]
        check(not leaked, f"{name} 没有写死闲鱼串（漏网：{leaked}）")
    check(
        sel.first_match("FAIL_SYS_TRAFFIC_LIMIT::被挤爆", sel.RATE_LIMIT_ANY_MARKERS) == "TRAFFIC",
        "限流标记优先命中",
    )
    check(sel.first_match("无关文本", sel.REDESIGN_MARKERS) == "", "first_match 不误报")


# ------------------------------------------------------------------------ fixtures
def test_parse_fixtures() -> None:
    """离线跑 tests/fixtures 的接口样本：改版、风控、限流都不必赌一次真实请求。

    文件名前缀即预期：normal_ 出商品、real_ 是真实抓包回放、risk_ 判风控、
    rate_ 判限流、broken_ 判改版。真跑过一轮 `scan --dump` 之后，把 data/captures
    里的样本改名成 normal_* 或 real_* 放进来。
    """
    print("fixtures")
    directory = ROOT / "tests" / "fixtures"
    files = sorted(directory.glob("*.json"))
    check(len(files) >= 5, f"解析样本至少 5 份（当前 {len(files)} 份）")

    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        expect = payload.pop("_expect", None) or {}
        kind = path.name.split("_", 1)[0]
        if kind in ("normal", "real"):
            listings, _ = parse_payload(payload)
            wanted = expect.get("listings")
            if kind == "real" and wanted is None:
                wanted = 30
            check(
                len(listings) == wanted,
                f"{path.name} 解析出 {wanted} 条（实际 {len(listings)}）",
            )
            check(
                all(x.item_id and x.title and x.price > 0 for x in listings),
                f"{path.name} 每条都有 ID/标题/价格",
            )
        elif kind in ("risk", "rate"):
            try:
                parse_payload(payload)
            except ApiRejected as exc:
                wanted = "risk" if kind == "risk" else "rate_limit"
                check(error_kind(str(exc)) == wanted, f"{path.name} 归类为 {wanted}")
                check(bool(exc.ret), f"{path.name} 保留原始 ret 供复盘")
            else:
                check(False, f"{path.name} 应该抛 ApiRejected")
        else:
            try:
                parse_payload(payload)
            except ParseError as exc:
                check(
                    bool(sel.first_match(str(exc), sel.REDESIGN_MARKERS)),
                    f"{path.name} 的错误话术带改版提示：{exc}",
                )
                check(error_kind(f"接口结构异常，{exc}") != "risk", f"{path.name} 不会被误判成风控")
            else:
                check(False, f"{path.name} 结构异常应该抛 ParseError")

    captures = sorted((CFG.data_dir / "captures").glob("*.json"))
    for path in captures:
        try:
            parse_payload(json.loads(path.read_text(encoding="utf-8")))
        except (ApiRejected, ParseError, ValueError, OSError):
            pass
    print(f"  · 真实抓取样本 {len(captures)} 份（data/captures/，可为空）")


def test_rate_limited_routing() -> None:
    """限流异常在引擎里单独成档，落进 rate_limit 组而不是搜索风控组。"""
    print("rate_routing")
    watch = CFG.watches[0]

    class ThrottledCrawler:
        stats = {"search_requests": 1}

        def search(self, *args, **kwargs):
            raise RateLimited("闲鱼提示「请求频率或额度已超限」，本轮停止")

    data = _RoundData()
    _search_all(CFG, ThrottledCrawler(), [watch], data, 2, None, lambda message: None)
    check(data.aborted, "限流立即结束本轮")
    check("频率限制：" in data.errors.get(watch.name, ""), "限流错误写明是频率限制")
    _clean, groups = classify_round([watch], data.collected, data.errors)
    check(len(groups["rate_limit"]) == 1 and not groups["search_risk"], "限流进独立档位")


# ------------------------------------------------------------------- sort / 提前停
def test_sort_and_early_stop() -> None:
    """排序参数、页面顺序反查、老货提前停的判定：全离线。"""
    print("sort_stop")

    def item(item_id: str, stamp: int) -> Listing:
        return Listing(item_id, "联想拯救者R9000P RTX3060 游戏本", 3300, "u", publish_ts=stamp)

    check(CFG.scan.newest_first is True, "config 默认点「新发布 → 最新」")
    check(CFG.scan.max_age_hours == 24, "config 默认开 24 小时客户端时间窗（D1）")
    check(CFG.scan.seller_review_hint == 100, "config 默认开卖家画像标注阈值（D2）")
    url = build_search_url("游戏本")
    check(url.startswith(sel.BASE_URL), "搜索地址仍指向闲鱼搜索页")
    check("sortType" not in url, "不再携带已被服务端忽略的 sortType 参数")
    check(quote("游戏本") in url, "关键词仍然被转义")

    newest = [item(f"n{index}", 1_700_000_000_000 - index * 60_000) for index in range(6)]
    oldest = list(reversed(newest))
    check(looks_newest_first(newest), "新→旧顺序判定为排序已落地")
    check(not looks_newest_first(oldest), "旧→新顺序判定为没落地")
    check(not looks_newest_first(newest[:3]), "样本太少不判")
    check(not looks_newest_first([item(f"z{index}", 0) for index in range(6)]), "没有时间戳不判")
    messy = [
        item("m1", 500), item("m2", 400), item("m3", 450),
        item("m4", 300), item("m5", 200), item("m6", 100),
    ]
    check(looks_newest_first(messy), "少量乱序仍算新→旧（80% 阈值）")

    known = {listing.item_id for listing in newest}
    check(should_stop_early(newest, known), "整页老货 + 顺序确认 → 提前停")
    check(not should_stop_early(newest, known - {"n5"}), "有一条没见过 → 继续翻页")
    check(not should_stop_early(oldest, known), "排序没落地 → 不许提前停")
    check(not should_stop_early(newest, set()), "库里还没数据 → 不停")

    watch = CFG.watches[0]

    class RecordingCrawler:
        stats = {"search_requests": 1}

        def __init__(self) -> None:
            self.received: set[str] | None = None

        def search(self, _watch, pages=None, stop_event=None, known_ids=None):
            self.received = set(known_ids or ())
            return []

    conn = connect(TMP / "sort_stop.db")
    try:
        scan_id = start_scan(conn, 1)
        upsert(conn, item("old-1", 1), neutral_analysis(), scan_id)

        live = RecordingCrawler()
        _search_all(CFG, live, [watch], _RoundData(), 2, None, lambda message: None, conn=conn)
        check(live.received == {"old-1"}, "真实抓取装载已见商品 ID")

        disabled = RecordingCrawler()
        off = replace(CFG, scan=replace(CFG.scan, stop_on_known=False))
        _search_all(off, disabled, [watch], _RoundData(), 2, None, lambda message: None, conn=conn)
        check(disabled.received == set(), "stop_on_known=false 时不装载")

        no_conn = RecordingCrawler()
        _search_all(CFG, no_conn, [watch], _RoundData(), 2, None, lambda message: None)
        check(no_conn.received == set(), "没给 conn（demo/自检路径）照旧不影响")
    finally:
        conn.close()


def test_newest_filter_click() -> None:
    """点「新发布 → 最新」：限定筛选面板、URL/新标签守卫、各降级分支。"""
    print("newest_click")
    logs: list[str] = []

    class FakeText:
        def __init__(self, owner: "FakePage", text: str, count: int = 1) -> None:
            self.owner = owner
            self.text = text
            self._count = count

        @property
        def first(self) -> "FakeText":
            return self

        def count(self) -> int:
            return self._count

        def evaluate(self, script: str) -> None:
            # 生产代码必须走 DOM click，绕开全屏图片遮罩的 actionability 拦截。
            if self.owner.fail:
                raise RuntimeError("click failed")
            self.owner.clicks.append(self.text)
            if self.owner.leave_on_click:
                self.owner.url = "https://www.goofish.com/item?id=1"
            if self.owner.open_tab_on_click:
                extra = FakeExtraPage()
                self.owner.opened_tabs.append(extra)
                self.owner.ctx.pages.append(extra)

    class FakePanel:
        def __init__(self, page: "FakePage", count: int = 1) -> None:
            self.page = page
            self._count = count

        @property
        def first(self) -> "FakePanel":
            return self

        def count(self) -> int:
            return self._count

        def get_by_text(self, text: str, exact: bool = True) -> FakeText:
            return FakeText(self.page, text)

        def click(self, timeout: int = 0) -> None:
            raise AssertionError("不应直接点筛选面板本身")

    class FakeExtraPage:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FakeCtx:
        def __init__(self) -> None:
            self.pages: list[FakeExtraPage] = []

    class FakePage:
        def __init__(
            self,
            state: bool | None,
            fail: bool = False,
            panel_count: int = 1,
            leave_on_click: bool = False,
            open_tab_on_click: bool = False,
        ) -> None:
            self.state = state
            self.fail = fail
            self.panel_count = panel_count
            self.leave_on_click = leave_on_click
            self.open_tab_on_click = open_tab_on_click
            self.url = "https://www.goofish.com/search?q=x"
            self.clicks: list[str] = []
            self.page_clicks: list[str] = []
            self.opened_tabs: list[FakeExtraPage] = []
            self.ctx = FakeCtx()

        def wait_for_selector(self, *args, **kwargs) -> None:
            return None

        def evaluate(self, script: str):
            return self.state

        def locator(self, selector: str) -> FakePanel:
            check(selector == sel.SEARCH_FILTER_PANEL, "排序点击只查筛选面板")
            return FakePanel(self, self.panel_count)

        def get_by_text(self, text: str, exact: bool = True) -> FakeText:
            # 页面上还有一个同名「最新」也不该被点到：这里记录全页定位器的误用。
            return FakeText(self, text)

    crawler = Crawler(CFG, logger=logs.append)
    with patch.object(
        Crawler, "_wait_capture_settled", lambda self, number, page=None, timeout_ms=0: {"new": 1}
    ):
        page = FakePage(False)
        result = crawler._apply_newest_filter(page, {"old": 1})
    check(
        page.clicks == [sel.NEW_PUBLISH_FILTER_TEXT, sel.NEW_PUBLISH_OPTION_TEXT],
        "未排序时只在筛选面板内依次点「新发布」「最新」",
    )
    check(result == {"new": 1}, "点完改用筛选后的结果")
    check(crawler._last_sort_status == "已点「新发布 → 最新」", "成功后记录排序状态")

    page = FakePage(True)
    check(
        crawler._apply_newest_filter(page, {"old": 1}) == {"old": 1} and page.clicks == [],
        "已经是「最新」时不再重复点",
    )
    check(crawler._last_sort_status.startswith("已是「最新」"), "已是「最新」也记录状态")

    page = FakePage(None)
    check(
        crawler._apply_newest_filter(page, {"old": 1}) == {"old": 1} and page.clicks == [],
        "找不到排序控件时沿用初始结果",
    )
    check("未找到排序控件" in crawler._last_sort_status, "找不到控件记录降级原因")
    check(any("没找到" in message for message in logs), "找不到控件会明确告警")

    page = FakePage(False, panel_count=0)
    check(
        crawler._apply_newest_filter(page, {"old": 1}) == {"old": 1} and page.clicks == [],
        "没有筛选面板时不做全页点击",
    )
    check("未找到筛选面板" in crawler._last_sort_status, "没有面板也记录降级原因")

    class MissingText:
        @property
        def first(self) -> "MissingText":
            return self

        def count(self) -> int:
            return 0

    class MissingPanel:
        def get_by_text(self, text: str, exact: bool = True) -> MissingText:
            return MissingText()

    check(
        Crawler._click_text_in_panel(MissingPanel(), sel.NEW_PUBLISH_OPTION_TEXT) is False,
        "面板内没有目标文本时返回 False，不盲目点击",
    )

    page = FakePage(False, fail=True)
    with patch.object(Crawler, "_check_page_state", lambda self, page: 0.0):
        check(
            crawler._apply_newest_filter(page, {"old": 1}) == {"old": 1},
            "点击失败时沿用初始结果",
        )
    check(crawler._last_sort_status == "点筛选失败，按默认排序", "点击失败记录状态")
    check(any("失败" in message for message in logs), "点击失败会明确告警")

    page = FakePage(False, fail=True)
    raised = False
    with patch.object(
        Crawler, "_check_page_state", side_effect=RiskControl("页面出现「人机验证」")
    ):
        try:
            crawler._apply_newest_filter(page, {"old": 1})
        except RiskControl:
            raised = True
    check(raised, "点击失败时仍会检查页面风控，不把验证页当普通降级")

    page = FakePage(False, leave_on_click=True)
    raised = False
    try:
        crawler._apply_newest_filter(page, {"old": 1})
    except CrawlError:
        raised = True
    check(raised, "点排序后离开搜索页立即停止本轮")
    check("离开搜索页" in crawler._last_sort_status, "离开搜索页会记录状态")

    page = FakePage(False, open_tab_on_click=True)
    crawler._ctx = page.ctx
    raised = False
    try:
        crawler._apply_newest_filter(page, {"old": 1})
    except CrawlError:
        raised = True
    finally:
        crawler._ctx = None
    check(raised and page.opened_tabs and page.opened_tabs[0].closed, "点开新标签会关闭并停止本轮")
    check("打开了新页面" in crawler._last_sort_status, "新标签也记录状态")

    off = replace(CFG, scan=replace(CFG.scan, newest_first=False))
    page = FakePage(False)
    check(
        Crawler(off, logger=logs.append)._apply_newest_filter(page, {"old": 1}) == {"old": 1}
        and page.clicks == [],
        "newest_first=false 时完全不碰页面",
    )


def test_search_loop_offline() -> None:
    """不启浏览器，假装页面动作，验证翻页循环真的会「老货提前停」少点一次下一页。"""
    print("search_loop")

    def payload(item_ids: list[str], stamps: list[int]) -> dict:
        return {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "resultList": [
                    {
                        "data": {
                            "item": {
                                "main": {
                                    "exContent": {
                                        "itemId": item_id,
                                        "title": "联想拯救者R9000P RTX3060 16G 512G 游戏本 自用",
                                        "price": [{"text": "3300"}],
                                        "area": "上海",
                                    },
                                    "clickParam": {"args": {"publishTime": str(stamp)}},
                                }
                            }
                        }
                    }
                    for item_id, stamp in zip(item_ids, stamps)
                ]
            },
        }

    base = 1_700_000_000_000
    falling = [base - step * 60_000 for step in range(6)]
    rising = list(reversed(falling))
    page_ids = {
        1: ["new-1"] + [f"k{index}" for index in range(5)],
        2: [f"k{index}" for index in range(6)],
        3: ["deep-1"] + [f"k{index}" for index in range(5)],
    }
    known = {f"k{index}" for index in range(6)}
    watch = Watch(name="游戏本", keyword="游戏本", pages=3)

    class FakePage:
        def __init__(self) -> None:
            self.visited: list[str] = []

        def goto(self, url, wait_until=None) -> None:
            self.visited.append(url)

        def close(self) -> None:
            pass

    class FakeCtx:
        def __init__(self) -> None:
            self.page = FakePage()

        def new_page(self) -> FakePage:
            return self.page

        def cookies(self) -> list[dict]:
            return [
                {"name": name, "value": "1"}
                for name in sel.LOGIN_COOKIES
            ]

    def run(
        stop_on_known: bool,
        ordered: bool = True,
        newest_first: bool | None = None,
    ) -> tuple[list[int], list[str], str]:
        stamps = falling if ordered else rising
        payloads = {number: payload(ids, stamps) for number, ids in page_ids.items()}
        scan = replace(CFG.scan, stop_on_known=stop_on_known)
        if newest_first is not None:
            scan = replace(scan, newest_first=newest_first)
        cfg = replace(
            CFG,
            scan=scan,
            browser=replace(CFG.browser, min_delay=0, max_delay=0),
        )
        advanced: list[int] = []
        logs: list[str] = []
        crawler = Crawler(cfg, logger=logs.append)
        ctx = FakeCtx()
        crawler._ctx = ctx
        with patch.object(Crawler, "_check_page_state", lambda self, page: 0.0), patch.object(
            Crawler, "_dismiss_ad_popup", lambda self, page: None
        ), patch.object(
            Crawler, "_wait_capture", lambda self, number, page=None: payloads[number]
        ), patch.object(
            Crawler, "_apply_newest_filter", lambda self, page, payload: payload
        ), patch.object(
            Crawler, "_advance", lambda self, page, number: advanced.append(number) or payloads[number]
        ):
            crawler.search(watch, known_ids=known)
        status = crawler.sort_status.get(watch.keyword, "")
        return advanced, ctx.page.visited + logs, status

    advanced, logs, status = run(True)
    check("sortType" not in logs[0], "搜索地址不再带无效排序参数")
    check(advanced == [2], "只翻到第 2 页，第 3 页没再点")
    check(any("提前结束翻页" in message for message in logs), "提前停会说明原因")
    check(status.endswith("顺序已确认新→旧"), "排序状态带上发布时间反查结果")

    advanced, _logs, _status = run(False)
    check(advanced == [2, 3], "开关关掉就照原样翻满")

    advanced, logs, status = run(True, ordered=False)
    check(advanced == [2, 3], "排序没落地时不许提前停")
    check(any("不是新→旧" in message for message in logs), "排序没落地会打一次提示")
    check("顺序未确认（返回不是新→旧）" in status, "排序没落地也会写进报表状态")

    advanced, logs, _status = run(True, ordered=False, newest_first=False)
    check(advanced == [2, 3], "关闭排序时同样不按老货提前停")
    check(
        not any("排序没生效" in message for message in logs),
        "明确关闭排序时不再误报「排序没生效」",
    )


# --------------------------------------------------------- login / Baxia / 页面状态
class _FakePopup:
    def __init__(self, counts: list[int]) -> None:
        self._counts = iter(counts)

    @property
    def first(self) -> "_FakePopup":
        return self

    def count(self) -> int:
        return next(self._counts, 0)


class _FakeStatePage:
    def __init__(
        self,
        text: str = "",
        url: str = "https://www.goofish.com/search?q=x",
        baxia: list[int] | None = None,
    ) -> None:
        self.text = text
        self.url = url
        self.popup = _FakePopup(baxia or [0])

    def locator(self, _selector: str) -> _FakePopup:
        return self.popup

    def inner_text(self, _selector: str, timeout: int = 0) -> str:
        return self.text


def test_page_state_and_baxia() -> None:
    print("page_state")
    crawler = Crawler(CFG, logger=lambda message: None)

    try:
        crawler._check_page_state(
            _FakeStatePage(text="请求频率或额度已超限，请完成验证")
        )
    except RateLimited:
        check(True, "限流页同时出现「请完成验证」时先判限流")
    else:
        check(False, "限流页应该抛 RateLimited")

    try:
        crawler._check_page_state(_FakeStatePage(text="请完成验证"))
    except RiskControl:
        check(True, "验证页抛 RiskControl")
    else:
        check(False, "验证页应该抛 RiskControl")

    try:
        crawler._check_page_state(_FakeStatePage(text="非法访问"))
    except CrawlError:
        check(True, "非法访问抛 CrawlError")
    else:
        check(False, "非法访问应该抛 CrawlError")

    try:
        crawler._check_page_state(
            _FakeStatePage(url="https://passport.goofish.com/login")
        )
    except LoginRequired:
        check(True, "跳登录页抛 LoginRequired")
    else:
        check(False, "跳登录页应该抛 LoginRequired")

    headless = Crawler(CFG, headless=True, logger=lambda message: None)
    try:
        headless._check_page_state(_FakeStatePage(baxia=[1]))
    except RiskControl:
        check(True, "无头模式遇到 Baxia 直接判风控")
    else:
        check(False, "无头模式应该抛 RiskControl")

    logs: list[str] = []
    headed = Crawler(CFG, logger=logs.append)
    with patch("xianyu.crawler.time.sleep", lambda _seconds: None):
        waited = headed._check_page_state(_FakeStatePage(baxia=[1, 0]))
    check(waited >= 0 and 0 < len(logs), "有头模式等主人滑过 Baxia 后继续")
    check(any("验证已通过" in message for message in logs), "Baxia 通过后会记录等待结果")


# --------------------------------------------------------------------------- report
def report_item(**overrides) -> dict:
    base = {
        "item_id": "1001",
        "title": "联想拯救者R9000P RTX3060 16G 512G 游戏本",
        "price": 3300.0,
        "url": "https://www.goofish.com/item?id=1001",
        "image": "",
        "location": "上海",
        "seller": "小陈同学",
        "seller_id": "u1puyGhmFvz4Wbw/sD6bKw==",
        "publish_ts": 0,
        "wants": 0,
        "tags": [],
        "watch": "游戏本",
        "status": "pending",
        "verdict": "待审核",
        "ref_price": 0.0,
        "ratio": 1.0,
        "reasons": ["细筛通过：主页 1 条"],
        "flags": [],
        "suspicious": 0,
        "score": 0.0,
        "first_seen": "2026-10-21 10:00:00",
        "last_seen": "2026-10-21 10:00:00",
        "notified_at": "",
        "note": "",
    }
    base.update(overrides)
    return base


def test_report() -> None:
    print("report")
    now_ms = int(time.time() * 1000)
    item = row_to_item(report_item(), now_ms)
    check(
        item["seller_url"]
        == "https://www.goofish.com/personal?userId=u1puyGhmFvz4Wbw%2FsD6bKw%3D%3D",
        "卖家主页链接对 ID 完整转义",
    )
    check("unchecked" not in item and "carried" not in item, "报表条目不再带上轮/未细筛字段")

    items = [
        {**row_to_item(report_item(), now_ms), "is_new": True},
        {**row_to_item(report_item(item_id="1002"), now_ms), "is_new": False},
    ]
    rejected = [
        rejected_item(
            Listing(
                "2001",
                "华强北数码城 拯救者R9000P RTX3060 16G",
                2100,
                "https://www.goofish.com/item?id=2001",
                seller="华强北数码城",
                seller_id="shop-1",
                watch="游戏本",
                keyword="游戏本",
            ),
            "疑似商家：数码城",
            now_ms,
        ),
        rejected_item(
            Listing(
                "2002",
                "联想拯救者R9000P RTX3060 无法开机",
                900,
                "https://www.goofish.com/item?id=2002",
                seller="小陈同学",
                watch="游戏本",
                keyword="游戏本",
            ),
            "开机故障：无法开机",
            now_ms,
        ),
    ]
    html_text = render_html(
        items,
        title="闲鱼游戏本 · 匿名粗筛",
        subtitle="搜索 9 条 · 粗筛通过 7 条",
        stats={
            "fetched": 9,
            "coarse": 7,
            "rejected": 2,
            "kept": 2,
            "sort_status": "游戏本：已点「新发布 → 最新」",
        },
        generated_at="2026-10-21 10:00:00",
        rejected=rejected,
    )
    check("参考价" not in html_text and "折价" not in html_text, "v3 报表不出现参考价/折价列")
    check("卖家主页" in html_text and items[0]["seller_url"] in html_text, "报表提供卖家主页链接")
    check("NEW" in html_text, "本轮新增标记")
    check("上轮遗留" not in html_text and "未细筛" not in html_text, "旧板块/标记已移除")
    check("不符合" in html_text and "无法开机" in html_text, "HTML 有不符合板块和原因")
    check("华强北数码城" in html_text and "疑似商家" in html_text, "不符合条目保留卖家与原因")
    check("细筛主页" not in html_text, "移除已废弃的细筛主页统计")
    check(
        "排序：" in html_text and "已点「新发布 → 最新」" in html_text,
        "HTML 报表记录筛选是否真正点下",
    )
    risky_html = render_html(
        items,
        title="闲鱼游戏本 · 匿名粗筛",
        subtitle="",
        stats={"sort_status": "游戏本：未找到排序控件，按默认排序"},
        generated_at="2026-10-21 10:00:00",
    )
    check("⚠" in risky_html and "按默认排序" in risky_html, "排序降级在 HTML 里高亮告警")

    md_text = render_markdown(
        items,
        title="闲鱼游戏本 · 匿名粗筛",
        generated_at="2026-10-21 10:00:00",
        sort_status="游戏本：已点「新发布 → 最新」",
        rejected=rejected,
    )
    check("参考价" not in md_text and "游戏本" in md_text, "Markdown 只列待审核商品")
    check("排序：游戏本：已点「新发布 → 最新」" in md_text, "Markdown 记录排序状态")
    check("## 不符合" in md_text and "开机故障：无法开机" in md_text, "Markdown 有不符合理由")
    check("上轮遗留" not in md_text and "未细筛" not in md_text, "Markdown 也清掉旧标记")

    paths = write_all(
        TMP / "reports",
        items,
        title="闲鱼游戏本 · 匿名粗筛",
        subtitle="搜索 9 条 · 粗筛通过 7 条",
        stats={
            "fetched": 9,
            "coarse": 7,
            "rejected": 2,
            "kept": 2,
            "top_n": 50,
            "sort_status": "游戏本：已点「新发布 → 最新」",
        },
        rejected=rejected,
    )
    for key in (
        "html",
        "latest_html",
        "md",
        "latest_md",
        "csv",
        "latest_csv",
        "rejects_csv",
        "latest_rejects_csv",
    ):
        check(paths[key].is_file(), f"报表文件存在：{key}")
    check(
        "排序：游戏本：已点「新发布 → 最新」"
        in paths["md"].read_text(encoding="utf-8"),
        "落盘 Markdown 报表带上筛选状态",
    )
    check(
        re.fullmatch(r"scan_\d{8}_\d{6}\.html", paths["html"].name) is not None,
        "报表文件名精确到秒，避免连续轮次互相覆盖",
    )
    csv_text = paths["csv"].read_text(encoding="utf-8-sig")
    check("卖家主页" in csv_text and "参考价" not in csv_text, "CSV 列与 v3 字段一致")
    check("上轮遗留" not in csv_text and "未细筛" not in csv_text, "CSV 不再输出旧列")
    reject_csv = paths["rejects_csv"].read_text(encoding="utf-8-sig")
    check("不符合原因" in reject_csv and "疑似商家" in reject_csv, "不符合 CSV 保留原因")

    # 外部可控文本进 CSV 前加前缀，防 Excel 公式注入
    evil_items = [
        {
            **row_to_item(report_item(item_id="9001"), now_ms),
            "title": '=HYPERLINK("http://evil.example","x")',
            "seller": "@SUM(1+1)",
            "location": "-2+3",
        }
    ]
    evil_csv = TMP / "csv_formula.csv"
    write_csv(evil_csv, evil_items)
    evil_text = evil_csv.read_text(encoding="utf-8-sig")
    check("'=HYPERLINK" in evil_text, "CSV 标题的 = 公式被转义")
    check("'@SUM(1+1)" in evil_text, "CSV 卖家的 @ 公式被转义")
    check("'-2+3" in evil_text, "CSV 地区的 - 公式被转义")
    evil_rejects = TMP / "csv_formula_rejects.csv"
    write_reject_csv(evil_rejects, [{**rejected[0], "title": "=1+1", "seller": "+1"}])
    evil_reject_text = evil_rejects.read_text(encoding="utf-8-sig")
    check("'=1+1" in evil_reject_text, "不符合 CSV 同样防公式注入")
    check("'+1" in evil_reject_text, "不符合 CSV 的 + 开头被转义")


def test_seller_and_problem_words() -> None:
    """v3.8.4 主人新增词表：8 个卖家筛选词 + 4 个问题机硬筛选词。"""
    print("seller & problem words")

    # 需求 1：卖家昵称命中即排除（每个词配一个不含旧词的昵称）
    seller_cases = [
        ("华硕经销部", "经销部"),
        ("华硕电脑", "电脑"),
        ("闲鱼严选", "严选"),
        ("华强北电子", "电子"),
        ("华硕包换", "包换"),
        ("阿呆二手", "二手"),
        ("极客科技", "科技"),
        ("阿呆小铺", "小铺"),
    ]
    for seller, word in seller_cases:
        reason = fatal_reject_reason(
            Listing("w-1", "联想拯救者R7000 RTX3060 16G 512G 游戏本", 3000, "u", seller=seller),
            CFG,
        )
        check(
            reason.startswith("疑似商家") and word in reason,
            f"卖家词「{word}」命中排除（{seller} → {reason}）",
        )

    # 需求 3：问题机词在标题/介绍命中即排除
    problem_cases = [
        ("联想拯救者R7000 问题机 当垃圾配件处理", "问题机"),
        ("拯救者R7000 进不去系统 随便卖卖", "进不去系统"),
        ("天选3 win转圈卡死 求秒", "win转圈卡死"),
        ("拯救者R9000P 处理配件 单出主板", "处理配件"),
    ]
    for title, word in problem_cases:
        listing = Listing("w-2", title, 3000, "u", seller="小陈同学")
        reason = coarse_reject(listing, CFG)
        check(reason == f"问题机：{word}", f"问题机词「{word}」进不符合（{reason}）")
        check(
            fatal_reject_reason(listing, CFG) == f"问题机：{word}",
            f"问题机词「{word}」在硬规则层命中（{word}）",
        )

    # 否定/正面描述不误伤（hit_words 通用能力，问题机词同样享受）
    ok = Listing(
        "w-3",
        "拯救者R7000 RTX3060 16G 512G 不是问题机 一切正常 进系统很快",
        3000,
        "u",
        seller="小陈同学",
        desc="不是问题机，无任何暗病",
    )
    check(coarse_reject(ok, CFG) == "", "「不是问题机」否定句不误伤")
    # 卖家词不受否定句影响（昵称是整体匹配）
    check(
        fatal_reject_reason(ok, CFG) == "",
        "普通卖家 + 否定句描述可通过硬规则",
    )


def test_report_ignore_button() -> None:
    """v3.8.4 报表「忽略」键：HTML 按钮 + 跨域调用审核页服务端到端。"""
    print("report ignore button")
    now_ms = int(time.time() * 1000)
    items = [
        {**row_to_item(report_item(), now_ms), "is_new": True},
        {**row_to_item(report_item(item_id="1002"), now_ms), "is_new": False},
    ]
    html_text = render_html(
        items,
        title="闲鱼游戏本 · 匿名粗筛",
        subtitle="",
        stats={"fetched": 2, "coarse": 2, "rejected": 0, "kept": 2},
        generated_at="2026-10-23 09:00:00",
    )
    check(
        html_text.count('data-status="ignored">忽略</button>') == len(items),
        "每条待审核都有忽略按钮",
    )
    check("button.ignore" in html_text or 'class="btn ignore"' in html_text, "忽略按钮带样式类")
    check("127.0.0.1:8765/api/status" in html_text, "忽略键指向本地审核页服务")
    custom_html = render_html(
        items,
        title="t",
        subtitle="",
        stats={},
        generated_at="",
        review_port=8899,
    )
    check(
        "127.0.0.1:8899/api/status" in custom_html,
        "忽略键端口可注入（跟随 config.review.port）",
    )
    check('id="cnt-pending"' in html_text and 'id="cnt-rejected"' in html_text, "计数带 id 供即时更新")
    check('id="sec-pending"' in html_text, "待审核板块计数带 id")
    check("tr.done" in html_text, "忽略成功后行置灰样式存在")
    check("忽略键" in html_text, "报表说明忽略键用法")

    # 端到端：起真审核页服务，验证 CORS 预检 + 忽略写库
    root = TMP / "ignore_btn"
    cfg = replace(CFG, root=root, watches=[CFG.watches[0]])
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    try:
        scan_id = start_scan(conn, 1)
        upsert(
            conn,
            Listing(
                "ig-1",
                "华硕天选2 RTX3050 16G 512G 游戏本",
                3600,
                "https://www.goofish.com/item?id=ig-1",
                seller="小陈同学",
                seller_id="seller-ig",
            ),
            neutral_analysis(),
            scan_id,
        )
        finish_scan(conn, scan_id, pages=1, fetched=1, kept=1)
    finally:
        conn.close()

    from xianyu.review import make_handler

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, cfg.db_path))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        # file:// 源发 JSON POST 会先预检
        client.request("OPTIONS", "/api/status", headers={"Origin": "null"})
        resp = client.getresponse()
        resp.read()
        check(resp.status == 204, "OPTIONS 预检返回 204")
        check(
            resp.getheader("Access-Control-Allow-Origin") == "null",
            "预检只对 file:// 源回显 Origin，静态报表可跨域",
        )
        # 任意网站发来的预检和写请求都必须拒绝
        client.request(
            "OPTIONS", "/api/status", headers={"Origin": "https://evil.example"}
        )
        resp = client.getresponse()
        resp.read()
        check(resp.status == 403, "外部源预检被拒绝")
        check(
            resp.getheader("Access-Control-Allow-Origin") is None,
            "被拒的外部源拿不到 CORS 头",
        )
        client.request(
            "POST",
            "/api/status",
            json.dumps({"item_id": "ig-1", "status": "ignored"}),
            {"Content-Type": "application/json", "Origin": "https://evil.example"},
        )
        resp = client.getresponse()
        resp.read()
        check(resp.status == 403, "外部源 POST 被拒绝（不能改库）")
        # Host 不是本机地址（DNS rebinding）也拒绝
        client.putrequest("GET", "/", skip_host=True)
        client.putheader("Host", "evil.example")
        client.putheader("Origin", "null")
        client.endheaders()
        resp = client.getresponse()
        resp.read()
        check(resp.status == 403, "非本机 Host 被拒绝")
        # 真忽略
        body = json.dumps({"item_id": "ig-1", "status": "ignored"})
        client.request(
            "POST",
            "/api/status",
            body,
            {"Content-Type": "application/json", "Origin": "null"},
        )
        resp = client.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        check(
            resp.getheader("Access-Control-Allow-Origin") == "null",
            "POST 响应回显允许的 Origin",
        )
        check(payload.get("ok") is True, f"报表忽略写库成功（{payload}）")
        check("不符合" in str(payload.get("message")), "忽略后提示移入不符合")
        client.close()

        conn = connect(cfg.db_path)
        try:
            row = conn.execute(
                "SELECT status, note FROM listings WHERE item_id='ig-1'"
            ).fetchone()
            check(row["status"] == "ignored", "忽略后 status=ignored")
            check(row["note"].startswith(MANUAL_REJECT_REASON), "忽略后 note 带人工审核前缀")
            check(
                len(rows_ignored(conn)) == 0,
                "人工忽略不进自动忽略板块（只进不符合）",
            )
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_price_range_filter() -> None:
    """v3.8.4 顶栏「价格区间」：待审核与不符合两个板块都按价格即时筛选。"""
    print("price range filter")
    now_ms = int(time.time() * 1000)
    items = [
        {**row_to_item(report_item(), now_ms), "is_new": True},
    ] + [
        {
            **row_to_item(
                report_item(item_id=str(1000 + index), price=price), now_ms
            ),
            "is_new": False,
        }
        for index, price in enumerate((1200.0, 4500.0, 8800.0), start=1)
    ]
    rejected = [
        reject_row_to_item(
            {
                "item_id": "rj-1",
                "title": "低配游戏本",
                "price": 900.0,
                "url": "https://www.goofish.com/item?id=rj-1",
                "image": "",
                "location": "广州",
                "seller": "小陈同学",
                "seller_id": "u1puyGhmFvz4Wbw/sD6bKw==",
                "publish_ts": 0,
                "watch": "游戏本",
                "keyword": "游戏本",
                "reason": "识别不到英伟达显卡",
            },
            now_ms,
        )
    ]
    html_text = render_html(
        items,
        title="闲鱼游戏本 · 匿名粗筛",
        subtitle="",
        stats={"fetched": 4, "coarse": 4, "rejected": 1, "kept": 4},
        generated_at="2026-10-23 09:20:00",
        rejected=rejected,
    )
    check("价格区间" in html_text, "顶栏有「价格区间」标签")
    check('id="pmin"' in html_text and 'id="pmax"' in html_text, "价格区间有两个输入框")
    check('id="pclear"' in html_text, "价格区间有清除按钮")
    check('id="filter-state"' in html_text, "有筛选状态提示位")
    check(
        html_text.count('data-price="') == len(items) + len(rejected),
        f"待审核与不符合每行都带 data-price（{len(items)}+{len(rejected)}）",
    )
    check('data-price="3300.0"' in html_text, "待审核行的价格为数值（供比较）")
    check('data-price="900.0"' in html_text, "不符合行也带价格（同样参与筛选）")
    check("dataset.price" in html_text, "筛选逻辑读取 data-price")
    check("xianyu_report_price_range" in html_text, "价格区间记在本机 localStorage")


def test_reject_persistence() -> None:
    """v3.8.4 修复：人工忽略 / 积压重筛的记录不能因为跑了新扫描就消失。

    「不符合」板块默认只读最新一轮的 scan_rejects，而人工忽略和重筛写在它们
    当时那一轮——不补回来的话，这些商品会掉进无人区（待审核、不符合、自动忽略
    都没有）。这里锁定跨轮保留行为。
    """
    print("reject persistence")
    root = TMP / "reject_persist"
    cfg = replace(CFG, root=root, watches=[CFG.watches[0]])
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    try:
        # 第 1 轮：播两个待审核
        scan1 = start_scan(conn, 1)
        for item_id in ("rp-1", "rp-2"):
            upsert(
                conn,
                Listing(
                    item_id,
                    "华硕天选2 RTX3050 16G 512G 游戏本",
                    3600,
                    f"https://www.goofish.com/item?id={item_id}",
                    seller="小陈同学",
                    seller_id=f"seller-{item_id}",
                ),
                neutral_analysis(),
                scan1,
            )
        finish_scan(conn, scan1, pages=1, fetched=2, kept=2)

        # 人工忽略 rp-1（写进当时的最新一轮 #1）
        check(mark_manual_reject(conn, "rp-1"), "人工忽略写入成功")
        # 重筛移出 rp-2（note 用重筛前缀）
        conn.execute(
            "UPDATE listings SET status='ignored', note=? WHERE item_id=?",
            (f"{RESCREEN_NOTE_PREFIX}疑似商家：电脑", "rp-2"),
        )
        conn.commit()

        # 第 2 轮：新扫描，最新一轮变成 #2
        scan2 = start_scan(conn, 1)
        finish_scan(conn, scan2, pages=1, fetched=0, kept=0)

        # 只按「本轮 scan_rejects」取 → 两条都丢了（回归前的 bug）
        fresh = [dict(row) for row in rows_rejects(conn, scan2)]
        check(len(fresh) == 0, f"新轮次本身没有不符合（{len(fresh)}）")

        # 跨轮保留后 → 两条都还在
        merged = [dict(row) for row in rows_rejects(conn, scan2)] + [
            ignored_row_to_reject_item(row, int(time.time() * 1000))
            for row in rows_manual_rejects(conn, scan2)
        ]
        reasons = [str(item.get("reason") or "") for item in merged]
        check(len(merged) == 2, f"跨轮保留后不符合仍是 2 条（{len(merged)}）")
        check(any(r == MANUAL_REJECT_REASON for r in reasons), "人工忽略跨轮保留")
        check(
            any(r.startswith(RESCREEN_NOTE_PREFIX) for r in reasons),
            "积压重筛移出的商品跨轮保留",
        )

        # 两个前缀都不进自动忽略板块
        check(len(rows_ignored(conn)) == 0, "人工/重筛记录都不进自动忽略板块")
    finally:
        conn.close()


def test_review_page() -> None:
    print("review page")
    cfg = replace(CFG, root=TMP / "review_page", watches=[CFG.watches[0]])
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    try:
        scan_id = start_scan(conn, 1)
        upsert(
            conn,
            Listing(
                "human-1",
                "华硕天选2 RTX3050 16G 512G 游戏本",
                3600,
                "https://www.goofish.com/item?id=human-1",
                seller="小陈同学",
                seller_id="seller-human",
            ),
            neutral_analysis(),
            scan_id,
        )
        check(mark_manual_reject(conn, "human-1"), "审核页人工忽略落库")
        finish_scan(conn, scan_id, pages=1, fetched=1, kept=0)
    finally:
        conn.close()

    from xianyu.review import _page

    page = _page(cfg, cfg.db_path)
    check('data-tab="pending"' in page and 'data-tab="rejects"' in page, "审核页有不符合标签")
    check('data-tab="ignored"' in page and "自动忽略" in page, "审核页保留自动忽略标签")
    check("人工审核" in page and "恢复待审核" in page, "不符合卡片显示人工审核原因与恢复键")
    check("华硕天选2 RTX3050" in page, "不符合卡片保留商品标题")
    check(page.count('data-id="human-1"') == 1, "人工不符合不再出现在自动忽略里")


def test_rescreen() -> None:
    """D3 积压重筛：现行规则套旧待审核，移出进专用轮次的不符合。"""
    print("rescreen")
    root = TMP / "rescreen"
    cfg = replace(
        CFG,
        root=root,
        watches=[CFG.watches[0]],
        scan=replace(CFG.scan, max_age_hours=24),
    )
    cfg.ensure_dirs()
    now_ms = int(time.time() * 1000)
    hour = 3_600_000
    conn = connect(cfg.db_path)
    try:
        scan_id = start_scan(conn, 1)
        seeds = [
            ("rs-keep", "联想拯救者R7000 RTX3060 16G 512G 游戏本", now_ms - hour, "小陈同学"),
            ("rs-stale", "联想拯救者R7000 RTX3060 16G 512G 游戏本", now_ms - 48 * hour, "小陈同学"),
            ("rs-nogpu", "游戏本 i7 16G 512G 高配办公本", now_ms - hour, "小陈同学"),
            ("rs-shop", "华硕天选2 RTX3050 16G 512G 游戏本", now_ms - hour, "笔记本专营店"),
        ]
        for item_id, title, publish, seller in seeds:
            upsert(
                conn,
                Listing(
                    item_id,
                    title,
                    3000,
                    f"https://www.goofish.com/item?id={item_id}",
                    seller=seller,
                    publish_ts=publish,
                ),
                neutral_analysis(),
                scan_id,
            )
        finish_scan(conn, scan_id, pages=1, fetched=4, kept=4)

        keep, move = plan_rescreen(conn, cfg, now_ms)
        keep_ids = {row["item_id"] for row in keep}
        move_map = {row["item_id"]: reason for row, reason in move}
        check(keep_ids == {"rs-keep"}, "规划：只有新鲜且合规的保留")
        check(move_map.get("rs-stale") == FRESHNESS_REASON, "超窗商品移出，原因=超过发布时间限制")
        check("识别不到英伟达显卡" in move_map.get("rs-nogpu", ""), "无 N 卡商品移出")
        check("专营店" in move_map.get("rs-shop", ""), "卖家词命中移出（疑似商家）")
        check(
            len(rows_pending(conn, 100)) == 4,
            "规划阶段只读，不写库",
        )

        stats = apply_rescreen(conn, cfg, now_ms)
        check(
            stats["scan_id"] is not None and stats["moved"] == 3 and stats["kept"] == 1,
            "落库统计：移出 3 保留 1",
        )
        round_row = conn.execute(
            "SELECT * FROM scans WHERE id=?", (stats["scan_id"],)
        ).fetchone()
        check("人工重筛" in (round_row["sort_status"] or ""), "专用轮次带重筛标记，不与真实轮混淆")
        check(
            (round_row["fetched"], round_row["kept"]) == (4, 1),
            "专用轮次记录 4 过 1 留",
        )
        rows = rows_rejects(conn, stats["scan_id"])
        check(len(rows) == 3, "移出商品写进该轮不符合明细")
        check(
            {row["reason"] for row in rows} >= {FRESHNESS_REASON, "识别不到英伟达显卡，排除"},
            "不符合原因按规则落库",
        )
        check(
            {row["item_id"] for row in rows_pending(conn, 100)} == {"rs-keep"},
            "移出后待审核只剩合规商品",
        )
        ignored_ids = {row["item_id"] for row in rows_ignored(conn, 100)}
        check(
            not (ignored_ids & {"rs-stale", "rs-nogpu", "rs-shop"}),
            "重筛移出不进自动忽略标签（避免和不符合板块重复展示）",
        )
        note = conn.execute(
            "SELECT note FROM listings WHERE item_id='rs-stale'"
        ).fetchone()["note"]
        check(
            note.startswith(RESCREEN_NOTE_PREFIX) and FRESHNESS_REASON in note,
            "note 带重筛前缀并保留规则原因",
        )
        check(
            conn.execute(
                "SELECT status FROM listings WHERE item_id='rs-keep'"
            ).fetchone()["status"]
            == "pending",
            "保留商品状态不动",
        )

        again = apply_rescreen(conn, cfg, now_ms)
        check(
            again["scan_id"] is None and again["moved"] == 0,
            "重复执行不建轮次、不移商品（幂等）",
        )
        check(len(rows_rejects(conn, stats["scan_id"])) == 3, "幂等：不符合明细不变")
    finally:
        conn.close()


# --------------------------------------------------------------------------- engine
def test_engine() -> None:
    print("engine")
    root = TMP / "engine"
    cfg = replace(
        CFG,
        root=root,
        watches=[CFG.watches[0]],
        review=replace(CFG.review, port=8899),
    )
    messages: list[str] = []

    first = run_round(cfg, demo=True, logger=messages.append)
    stats = first.stats
    check(first.errors == [], "演示轮次没有错误")
    check(stats["fetched"] == 9, "演示数据 9 条")
    check(stats["coarse"] == 7, "粗筛后 7 条")
    check(stats["rejected"] == 2, "粗筛不符合 2 条")
    check(stats["kept"] == 7 and stats["new"] == 7, "首轮 7 条待审核")
    check(stats["pricing"] is False, "默认不启用价格筛选")
    check(
        stats["sort_status"] == "未记录" and first.sort_status == "未记录",
        "演示模式没有抓取，排序状态明确记为未记录",
    )
    check(
        all(item["reasons"][0] == "粗筛通过（搜索页商家判断）" for item in first.items),
        "方案二不再打开卖家主页，理由只到粗筛",
    )
    check(
        all("unchecked" not in item for item in first.items),
        "待审核条目没有未细筛标记",
    )

    conn = connect(cfg.db_path)
    try:
        check(len(rows_pending(conn, 1000)) == 7, "数据库里有 7 条待审核")
        check(len(load_seller_checks(conn)) == 0, "方案二不写卖家检查缓存")
        scan = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        check(scan["fetched"] == 9 and scan["kept"] == 7, "扫描记录写入统计")
        check(scan["sort_status"] == "未记录", "扫描记录同步排序状态")
        rejected_rows = rows_rejects(conn, int(scan["id"]))
        check(len(rejected_rows) == 2, "不符合商品单独落库")
        check(
            {row["reason"].split("：")[0] for row in rejected_rows} == {"疑似商家", "开机故障"},
            "不符合原因分类正确",
        )
        check(
            conn.execute("SELECT COUNT(*) FROM listings WHERE seller='华强北数码城'").fetchone()[0] == 0,
            "粗筛排除的商家商品不落待审核库",
        )
        check(
            conn.execute("SELECT COUNT(*) FROM listings WHERE title LIKE '%无法开机%'").fetchone()[0] == 0,
            "无法开机的商品不落待审核库",
        )
    finally:
        conn.close()

    check(first.paths["html"].is_file() and first.paths["latest_html"].is_file(), "本轮报表立即生成")
    first_html = first.paths["latest_html"].read_text(encoding="utf-8")
    check("不符合" in first_html and "无法开机" in first_html, "本轮报表带不符合板块")
    check(
        "127.0.0.1:8899/api/status" in first_html,
        "报表忽略键使用 config.review.port，而不是写死 8765",
    )

    second = run_round(cfg, demo=True, logger=messages.append)
    stats = second.stats
    check(stats["kept"] == 7 and stats["new"] == 0, "第二轮没有新增待审核")
    check(all(not item["is_new"] for item in second.items), "第二轮待审核都不是新增")
    check(
        "fine_requests" not in stats or stats.get("fine_requests", 0) == 0,
        "统计里没有细筛请求",
    )

    latest_before = second.paths["latest_html"].read_text(encoding="utf-8")
    stop = threading.Event()
    stop.set()
    stopped = run_round(cfg, demo=True, stop_event=stop, logger=messages.append)
    check(stopped.stopped, "stop 事件让本轮标记为已停止")
    check(stopped.stats["kept"] == 7 and stopped.stats["new"] == 0, "stop 时保留已有待审核")
    check(stopped.paths == {}, "stop 轮次不生成新报表")
    check(
        second.paths["latest_html"].read_text(encoding="utf-8") == latest_before,
        "stop 不覆盖 latest 报表",
    )

    watch = CFG.watches[0]
    clean, groups = classify_round([watch], {"游戏本": [make_listing("游戏本 RTX3060")]}, {})
    check(clean and not any(groups.values()), "有结果且无错误算完全干净")
    clean, groups = classify_round([watch], {}, {})
    check(not clean and groups["soft"] == ["全部关键词返回 0 条"], "0 条结果记为软失败")
    clean, groups = classify_round([watch], {}, {"游戏本": "风控：人机验证"})
    check(not clean and groups["search_risk"] == ["风控：人机验证"], "搜索风控归类正确")
    clean, _ = classify_round(
        [watch], {"游戏本": [make_listing("游戏本 RTX3060")]}, {}, stopped=True
    )
    check(not clean, "用户 stop 的轮次不算完全干净")


def test_real_demo() -> None:
    print("real demo")
    fixture = ROOT / "tests" / "fixtures" / "real_search_20261022.json"
    watch = CFG.watches[0]
    collected = load_saved_listings(fixture, [watch])
    check(len(collected[watch.name]) == 30, "真实抓包解析出 30 条")
    check(all(item.watch == watch.name and item.keyword == watch.keyword for item in collected[watch.name]),
          "回放时补上监控项和关键词")

    root = TMP / "real_demo"
    # 回放是冻结快照：固定关掉时间窗，保证计数稳定（时间窗由 test_freshness 单独覆盖）
    cfg = replace(CFG, root=root, watches=[watch], scan=replace(CFG.scan, max_age_hours=0))
    messages: list[str] = []
    outcome = run_round(
        cfg,
        demo=True,
        demo_file=fixture,
        logger=messages.append,
    )
    stats = outcome.stats
    check(stats["fetched"] == 30, "真实 demo 搜索 30 条")
    check(stats["coarse"] == 0, "真实 demo 粗筛通过 0 条（这批全是办公本/无 N 卡）")
    check(stats["rejected"] == 30, "真实 demo 不符合 30 条")
    check(stats["kept"] == 0 and stats["new"] == 0, "真实 demo 待审核 0 条")
    check(any("回放真实抓包" in message for message in messages), "终端说明这是真实抓包回放")
    check(not (cfg.data_dir / "risk_guard.json").exists(), "回放不写真实轮次的风控状态")

    conn = connect(cfg.db_path)
    try:
        scan = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        rows = rows_rejects(conn, int(scan["id"]))
        check(len(rows) == 30, "真实 demo 的不符合明细落库")
        sellers = {row["seller"] for row in rows}
        check(
            {"小白精品二手笔记本", "星辰精选", "圆满小店"} <= sellers,
            "新卖家排除词在真实样本里生效",
        )
        check(all(row["reason"] for row in rows), "每条不符合都记录了原因")
    finally:
        conn.close()

    html_text = outcome.paths["latest_html"].read_text(encoding="utf-8")
    check("不符合" in html_text and "小白精品二手笔记本" in html_text, "真实 demo 报表展示不符合板块")
    check("未细筛" not in html_text and "上轮遗留" not in html_text, "真实 demo 报表没有旧板块")


def test_real_1day() -> None:
    print("real 1day demo")
    fixture = ROOT / "tests" / "fixtures" / "real_search_1day_20261022.json"
    watch = CFG.watches[0]
    collected = load_saved_listings(fixture, [watch])
    items = collected[watch.name]
    check(len(items) == 30, "1天内 真实抓包解析出 30 条")
    stamps = [item.publish_ts for item in items]
    check(
        max(stamps) - min(stamps) <= 24 * 3600 * 1000,
        "1天内 真实抓包全部落在 24 小时窗口内",
    )
    check(
        any(left < right for left, right in zip(stamps, stamps[1:])),
        "1天内 只限定时间窗，返回顺序不保证新→旧",
    )

    root = TMP / "real_1day"
    # 回放是冻结快照：固定关掉时间窗，保证 3/27 计数稳定（时间窗由 test_freshness 单独覆盖）
    cfg = replace(CFG, root=root, watches=[watch], scan=replace(CFG.scan, max_age_hours=0))
    outcome = run_round(cfg, demo=True, demo_file=fixture, logger=lambda _: None)
    stats = outcome.stats
    check(stats["fetched"] == 30, "1天内 demo 搜索 30 条")
    check(stats["coarse"] == 3, "1天内 demo 粗筛通过 3 条")
    check(stats["rejected"] == 27, "1天内 demo 不符合 27 条")
    check(stats["kept"] == 3 and stats["new"] == 3, "1天内 demo 待审核 3 条")

    # 回放内部冻结时间窗：即使配置开着窗口（哪怕极小），快照计数也不随日历漂移
    frozen_messages: list[str] = []
    freeze_cfg = replace(
        CFG,
        root=TMP / "real_1day_freeze",
        watches=[watch],
        scan=replace(CFG.scan, max_age_hours=0.000001),
    )
    frozen = run_round(
        freeze_cfg, demo=True, demo_file=fixture, logger=frozen_messages.append
    )
    check(
        frozen.stats["coarse"] == 3 and frozen.stats["rejected"] == 27,
        "回放冻结时间窗：配置窗口极小也按快照原口径计数",
    )
    check(
        any("时间窗按快照口径冻结" in message for message in frozen_messages),
        "回放终端说明时间窗已冻结",
    )

    # D2 卖家画像：零额外请求，从同一份抓包解析
    signals = {
        item.seller: (item.seller_reviews, item.seller_rating, item.seller_identity)
        for item in items
    }
    check(signals.get("信誉为本") == (1538, 65, ""), "D2：信誉为本 1538 条评价 / 好评率 65%")
    check(signals.get("土味Boy") == (724, 56, ""), "D2：土味Boy 724 条评价 / 56%")
    check(signals.get("月夜西辞") == (0, 0, ""), "D2：0 条评价的个人卖家按无信号处理")
    check(
        signals.get("华中二手笔记本严选仓", ("", "", ""))[2] == "闲鱼严选卖家",
        "D2：严选卖家身份徽标解析",
    )

    conn = connect(cfg.db_path)
    try:
        scan = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        rows = rows_rejects(conn, int(scan["id"]))
        check(len(rows) == 27, "1天内 demo 的不符合明细落库")
        reasons = [row["reason"] for row in rows]
        check(any("笔记本" in reason for reason in reasons), "「笔记本」卖家词在 1天内 样本里生效")
        check(any("批发" in reason for reason in reasons), "「批发」卖家词在 1天内 样本里生效")
        reason_by_seller = {row["seller"]: row["reason"] for row in rows}
        check(
            "优品" in reason_by_seller.get("爱转优品", "")
            and "小卖部" in reason_by_seller.get("神舟战神小卖部", "")
            and "购物" in reason_by_seller.get("购物天堂", ""),
            "新卖家排除词在 1天内 样本里生效",
        )
    finally:
        conn.close()


def test_real_newest() -> None:
    """D1 真实铁证：点「新发布 → 最新」后的抓包（2026-10-23 真实轮次 #6 存档）。"""
    print("real newest demo")
    fixture = ROOT / "tests" / "fixtures" / "real_search_newest_20261023.json"
    watch = CFG.watches[0]
    collected = load_saved_listings(fixture, [watch])
    items = collected[watch.name]
    check(len(items) == 30, "最新排序真实抓包解析出 30 条")

    # D1：点「最新」后服务端记为排序（不是筛选），且返回严格新→旧
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    sqi = raw.get("data", {}).get("resultInfo", {}).get("sqiControlFields", {})
    check(
        sqi.get("hasSortWhenFirstPage") is True,
        "点「最新」后服务端 hasSort=True（记为排序，不是 1天内 那样的筛选）",
    )
    stamps = [item.publish_ts for item in items]
    check(
        all(left >= right for left, right in zip(stamps, stamps[1:])),
        "最新排序真实抓包 29/29 相邻对新→旧（1天内 样本只有 14/29）",
    )

    root = TMP / "real_newest"
    # 回放冻结快照：固定关掉时间窗，保证计数稳定（时间窗由 test_freshness 单独覆盖）
    cfg = replace(CFG, root=root, watches=[watch], scan=replace(CFG.scan, max_age_hours=0))
    outcome = run_round(cfg, demo=True, demo_file=fixture, logger=lambda _: None)
    stats = outcome.stats
    check(stats["fetched"] == 30, "最新 demo 搜索 30 条")
    check(stats["coarse"] == 12, "最新 demo 粗筛通过 12 条")
    check(stats["rejected"] == 18, "最新 demo 不符合 18 条")
    check(stats["kept"] == 12 and stats["new"] == 12, "最新 demo 待审核 12 条")

    # v3.8.4 新卖家词在真实样本里生效：二手 / 科技 各排下 1 条
    conn = connect(root / "data" / "xianyu.db")
    try:
        latest = conn.execute("SELECT id FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        reject_rows = rows_rejects(conn, int(latest["id"]))
        reasons = [row["reason"] for row in reject_rows]
    finally:
        conn.close()
    check(
        any("疑似商家：二手" in reason for reason in reasons),
        "v3.8.4 卖家词「二手」在真实样本里生效",
    )
    check(
        any("疑似商家：科技" in reason for reason in reasons),
        "v3.8.4 卖家词「科技」在真实样本里生效",
    )

    # 回放内部冻结时间窗：配置开着极小窗口也按快照口径计数
    freeze_cfg = replace(
        CFG,
        root=TMP / "real_newest_freeze",
        watches=[watch],
        scan=replace(CFG.scan, max_age_hours=0.000001),
    )
    frozen = run_round(freeze_cfg, demo=True, demo_file=fixture, logger=lambda _: None)
    check(
        frozen.stats["coarse"] == 12 and frozen.stats["rejected"] == 18,
        "回放冻结时间窗：最新 demo 计数不随日历漂移",
    )

    # D2 卖家画像：同一份抓包零额外请求解析
    signals = {item.seller: (item.seller_reviews, item.seller_rating) for item in items}
    check(
        signals.get("长期有货需要什么联系") == (788, 78),
        "D2：最新样本最高评价数卖家 788 条 / 好评率 78%",
    )
    check(
        sum(1 for item in items if item.seller_reviews >= 100) == 5,
        "D2：5 个卖家达到疑似职业卖家标注阈值",
    )

    conn = connect(cfg.db_path)
    try:
        scan = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        rows = rows_rejects(conn, int(scan["id"]))
        check(len(rows) == 18, "最新 demo 的不符合明细落库")
        reasons = [row["reason"] for row in rows]
        check(
            sum("识别不到英伟达显卡" in reason for reason in reasons) == 8,
            "最新样本 8 条因识别不到 N 卡被排除",
        )
        reason_by_seller = {row["seller"]: row["reason"] for row in rows}
        check("数码" in reason_by_seller.get("飞耀数码", ""), "「数码」卖家词在最新样本里生效")
        check("笔记本" in reason_by_seller.get("笔记本电脑", ""), "「笔记本」卖家词在最新样本里生效")
    finally:
        conn.close()


def test_freshness() -> None:
    """D1 客户端时间窗：超过 scan.max_age_hours 的商品在粗筛前被滤掉。"""
    print("freshness")
    now = int(time.time() * 1000)
    hour = 3_600_000

    def aged(item_id: str, hours: float) -> Listing:
        listing = make_listing(
            "联想拯救者R9000P RTX3060 16G 512G 游戏本", item_id=item_id
        )
        listing.publish_ts = 0 if hours < 0 else int(now - hours * hour)
        return listing

    fresh = aged("f1", 2)
    stale = aged("s1", 48)
    unknown = aged("u1", -1)

    check(is_fresh(fresh, 24, now), "2 小时前发布的商品通过 24 小时窗")
    check(not is_fresh(stale, 24, now), "48 小时前发布的商品被时间窗滤掉")
    check(is_fresh(stale, 0, now), "max_age_hours=0 时完全不过滤")
    check(is_fresh(unknown, 24, now), "发布时间未知时不过滤（宁可靠人看）")

    cfg = replace(CFG, scan=replace(CFG.scan, max_age_hours=24))
    kept, reasons, rejected = _coarse_filter(
        cfg, {"游戏本": [fresh, stale, unknown]}, now, lambda _: None
    )
    check([item.item_id for item in kept] == ["f1", "u1"], "时间窗只滤掉超窗商品")
    check(reasons.get("s1") == "超过发布时间限制", "超窗原因写明超过发布时间限制")
    check(
        any(
            listing.item_id == "s1" and reason == "超过发布时间限制"
            for listing, reason in rejected
        ),
        "超窗商品进不符合明细",
    )

    off = replace(CFG, scan=replace(CFG.scan, max_age_hours=0))
    kept_off, _reasons_off, _rejected_off = _coarse_filter(
        off, {"游戏本": [stale]}, now, lambda _: None
    )
    check([item.item_id for item in kept_off] == ["s1"], "关掉时间窗后超窗商品回到粗筛流程")

    # 时间窗只影响展示：超窗旧待审核沉底，不因再次被抓到而自动忽略（D4）
    root = TMP / "freshness_ignore"
    db_cfg = replace(CFG, root=root, watches=[CFG.watches[0]])
    db_cfg.ensure_dirs()
    conn = connect(db_cfg.db_path)
    try:
        first = start_scan(conn, 1)
        upsert(conn, aged("backlog-stale", 48), neutral_analysis(), first)
        upsert(conn, aged("backlog-empty", 1), neutral_analysis(), first)
        finish_scan(conn, first, pages=1, fetched=2, kept=2)
        second = start_scan(conn, 1)
        data = _RoundData(
            coarse_reasons={
                "backlog-stale": FRESHNESS_REASON,
                "backlog-empty": "空机",
            }
        )
        _finalize(
            db_cfg,
            conn,
            second,
            data,
            [CFG.watches[0]],
            pages=0,
            elapsed=0,
            logger=lambda _: None,
        )
        statuses = {
            row["item_id"]: row["status"]
            for row in conn.execute(
                "SELECT item_id, status FROM listings WHERE item_id LIKE 'backlog-%'"
            )
        }
        check(
            statuses.get("backlog-stale") == "pending",
            "超窗旧待审核只沉底，不被自动忽略",
        )
        check(
            statuses.get("backlog-empty") == "ignored",
            "其他粗筛原因仍按新规则自动忽略旧待审核",
        )
    finally:
        conn.close()


def test_review_signals_and_order() -> None:
    """D2 卖家画像标注 + D4 过期沉底：审核页渲染、排序与静态报表。"""
    print("review_signals")
    from xianyu.review import _page, _pending_order

    root = TMP / "review_signals"
    cfg = replace(
        CFG,
        root=root,
        watches=[CFG.watches[0]],
        scan=replace(CFG.scan, max_age_hours=24, seller_review_hint=100),
    )
    cfg.ensure_dirs()
    now_ms = int(time.time() * 1000)
    conn = connect(cfg.db_path)
    try:
        scan_id = start_scan(conn, 1)
        rows = [
            ("fresh-1", now_ms - 1 * 3600_000, 1538, 65, ""),
            ("fresh-2", now_ms - 5 * 3600_000, 0, 0, ""),
            ("stale-1", now_ms - 48 * 3600_000, 50, 70, ""),
            ("unknown-1", 0, 0, 0, ""),
            ("identity-1", now_ms - 2 * 3600_000, 190, 69, "闲鱼严选卖家"),
        ]
        for item_id, ts, reviews, rating, identity in rows:
            upsert(
                conn,
                Listing(
                    item_id,
                    "华硕天选4 RTX4060 16G 512G 游戏本",
                    4300,
                    f"https://www.goofish.com/item?id={item_id}",
                    seller=f"卖家-{item_id}",
                    seller_id=f"seller-{item_id}",
                    publish_ts=ts,
                    seller_reviews=reviews,
                    seller_rating=rating,
                    seller_identity=identity,
                ),
                neutral_analysis(),
                scan_id,
            )
        finish_scan(conn, scan_id, pages=1, fetched=5, kept=5)
    finally:
        conn.close()

    page = _page(cfg, cfg.db_path)
    positions = [page.find(f'data-id="{item_id}"') for item_id, *_ in rows]
    check(all(p >= 0 for p in positions), "5 条待审核都渲染出来")
    order = sorted(range(len(rows)), key=lambda index: positions[index])
    check(
        order == [0, 4, 1, 2, 3],
        "D4：时间窗内按发布时间新→旧，过期沉底，发布时间未知排最后",
    )
    check("疑似职业卖家：1538 条评价" in page, "D2：高评价数标注疑似职业卖家")
    check("闲鱼严选卖家" in page, "D2：严选身份徽标展示")
    check("评价 1538 · 好评率 65%" in page, "D2：评价数/好评率原文标注")
    check("疑似职业卖家：50 条评价" not in page, "D2：低评价数不标注")

    off_cfg = replace(cfg, scan=replace(cfg.scan, seller_review_hint=0))
    off_page = _page(off_cfg, cfg.db_path)
    check("疑似职业卖家" not in off_page, "D2：seller_review_hint=0 关闭疑似职业卖家标注")
    check("评价 1538 · 好评率 65%" in off_page, "D2：关闭标注后原文信息仍展示")

    key = _pending_order(
        {"publish_ts": now_ms - 3600_000}, now_ms, 24
    )
    check(key[0] == 0, "D4：1 小时前算时间窗内")
    check(
        _pending_order({"publish_ts": now_ms - 48 * 3600_000}, now_ms, 24)[0] == 1,
        "D4：48 小时前算过期",
    )
    check(_pending_order({"publish_ts": 0}, now_ms, 24)[0] == 2, "D4：时间未知排最后")
    check(
        _pending_order({"publish_ts": 1}, now_ms, 0)[0] == 0,
        "D4：max_age_hours=0 时没有过期组",
    )

    # 静态报表与 CSV 同步展示
    items = [
        row_to_item(
            {
                "item_id": "r1",
                "title": "华硕天选4 RTX4060 游戏本",
                "price": 4300,
                "url": "https://www.goofish.com/item?id=r1",
                "seller": "信誉为本",
                "seller_id": "seller-r1",
                "seller_reviews": 1538,
                "seller_rating": 65,
                "reasons": ["粗筛通过"],
                "flags": [],
                "tags": [],
                "status": "pending",
                "first_seen": "2026-10-23 00:00:00",
                "pro_seller": True,
            },
            now_ms,
        )
    ]
    html_text = render_html(
        items, title="t", subtitle="s", stats={}, generated_at="now"
    )
    check("疑似职业卖家：1538 条评价" in html_text, "D2：静态报表标注疑似职业卖家")
    check("评价 1538 · 好评率 65%" in html_text, "D2：静态报表展示评价数/好评率")
    check(is_pro_seller(items[0], 100) and not is_pro_seller(items[0], 0), "D2：阈值可关")
    check(seller_signal_note(items[0]) == "评价 1538 · 好评率 65%", "D2：标注文本格式")
    check(seller_signal_note({"seller_reviews": 0}) == "", "D2：无信号时标注为空")

    csv_path = TMP / "review_signals" / "sig.csv"
    write_csv(csv_path, items)
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    check("评价数" in csv_text and "1538" in csv_text, "D2：CSV 带评价数列")


def test_engine_budget() -> None:
    print("engine budget")
    root = TMP / "engine_budget"
    cfg = replace(
        CFG,
        root=root,
        watches=[CFG.watches[0]],
        scan=replace(CFG.scan, daily_request_budget=10),
    )
    messages: list[str] = []

    class CountingCrawler:
        def __init__(self, count: int):
            self.count = count
            self.stats = {"search_requests": 0}
            self.sort_status: dict[str, str] = {}

        def search(self, watch, **kwargs):
            self.stats["search_requests"] += self.count
            self.sort_status[watch.keyword] = "已点「新发布 → 最新」"
            return [make_listing("游戏本 RTX3060 16G 512G")]

    first = run_round(cfg, demo=False, crawler=CountingCrawler(3), logger=messages.append)
    check(
        DailyBudget(budget_path(cfg.data_dir), 10).used == 3,
        "引擎按本轮发出的搜索请求累加落盘预算",
    )
    check(
        "已点「新发布 → 最新」" in first.stats["sort_status"],
        "真实抓取路径把排序状态带进本轮报告",
    )
    run_round(cfg, demo=False, crawler=CountingCrawler(7), logger=messages.append)
    check(DailyBudget(budget_path(cfg.data_dir), 10).exhausted(), "跨轮累计到预算上限")

    raised = False
    try:
        run_round(cfg, demo=False, crawler=CountingCrawler(1), logger=messages.append)
    except ScanPaused as exc:
        raised = "每日请求预算" in str(exc)
    check(raised, "预算用尽后引擎在发请求前拒绝新一轮")

    # 即时计数：请求一发出就落盘，轮次被打断也不能少算。
    root2 = TMP / "engine_budget_live"
    cfg2 = replace(
        CFG,
        root=root2,
        watches=[CFG.watches[0]],
        scan=replace(CFG.scan, daily_request_budget=10),
    )

    class InterruptCrawler:
        def __init__(self):
            self.stats = {"search_requests": 0}
            self.sort_status: dict[str, str] = {}
            self.on_request = None

        def search(self, watch, **kwargs):
            self.stats["search_requests"] += 1
            self.on_request()
            raise KeyboardInterrupt

    interrupted = False
    try:
        run_round(cfg2, demo=False, crawler=InterruptCrawler(), logger=messages.append)
    except KeyboardInterrupt:
        interrupted = True
    check(interrupted, "中断异常照常向上抛出")
    check(
        DailyBudget(budget_path(cfg2.data_dir), 10).used == 1,
        "请求发出即计入预算，轮次中断也不丢计数",
    )
    conn = connect(cfg2.db_path)
    try:
        check(latest_scan(conn) is None, "被中断的轮次未收尾，不算最新扫描")
    finally:
        conn.close()


# ------------------------------------------------------------------ pricing retained
def test_pricing_retained() -> None:
    print("pricing retained")
    now = int(time.time() * 1000)
    watch = CFG.watches[0]

    price, name, source = match_reference(
        "联想拯救者R9000P 2021 RTX3060 16G", "3060", FIXTURE_ROWS
    )
    check(price == 3650 and source == "table/model", f"行情表匹配仍然可用：{name} {price}")

    good = analyze(
        Listing("p1", "联想拯救者R9000P RTX3060 16G 512G 游戏本", 3300, "u"),
        watch,
        FIXTURE_ROWS,
        CFG,
        now,
    )
    check(not good.excluded and good.ref_price == 3650, "开启比价后正常商品仍能通过")

    no_gpu = analyze(
        Listing("p2", "游戏本 i7 16G 512G 高配", 3300, "u"),
        watch,
        FIXTURE_ROWS,
        CFG,
        now,
    )
    check(no_gpu.excluded and any("英伟达" in flag for flag in no_gpu.flags), "旧 N 卡闸门保留")

    gap = analyze(
        Listing("p3", "联想拯救者R9000P RTX3060 16G 512G 游戏本", 3600, "u"),
        watch,
        FIXTURE_ROWS,
        CFG,
        now,
    )
    check(gap.excluded and any("100" in flag for flag in gap.flags), "旧价差底线保留")

    check(not gate(make_listing("MacBook Pro 14 M1 Pro"), CFG).ok, "旧品牌/品类闸门保留")
    check(CFG.pricing.enabled is False, "v3 默认关闭价格筛选")


# ------------------------------------------------------------------------------ cli
def test_cli() -> None:
    print("cli")
    check(
        __version__ == (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        f"版本号与 VERSION 文件一致（{__version__}）",
    )
    check("方案二" in BANNER, "横幅标明方案二")

    parser = build_parser()
    args = parser.parse_args(["scan", "--demo"])
    check(args.command == "scan" and args.demo is True, "scan --demo 仍可用")
    args = parser.parse_args(["scan", "--headed"])
    check(args.headed is True, "scan 支持 --headed")
    args = parser.parse_args(["scan", "--dump"])
    check(args.dump is True, "scan 支持 --dump 落盘真实接口样本")
    args = parser.parse_args(
        ["scan", "--demo-file", "tests/fixtures/real_search_20261022.json"]
    )
    check(
        args.demo_file == "tests/fixtures/real_search_20261022.json",
        "scan 支持 --demo-file 回放真实抓包",
    )
    check(
        _scan_db_path(CFG, demo=True, demo_file=None) == CFG.data_dir / "demo.db",
        "合成演示写 demo.db",
    )
    check(
        str(_scan_db_path(CFG, demo=False, demo_file=Path("real.json"))) == ":memory:",
        "真实回放用临时内存库，不污染 demo.db",
    )
    check(
        str(_scan_db_path(CFG, demo=True, demo_file=Path("real.json"))) == ":memory:",
        "--demo 与 --demo-file 同给时按回放处理",
    )
    check(
        _scan_db_path(CFG, demo=False, demo_file=None) is None,
        "真实扫描沿用正式库",
    )
    args = parser.parse_args(["loop", "--rounds", "2"])
    check(args.command == "loop" and args.rounds == 2, "loop 命令可用（纯手动 next）")
    args = parser.parse_args(["loop", "--no-notify"])
    check(args.no_notify is True, "loop 支持 --no-notify")
    args = parser.parse_args(["report", "--demo"])
    check(args.command == "report" and args.demo is True, "report --demo 可用")
    args = parser.parse_args(["login", "--timeout", "60"])
    check(args.command == "login" and args.timeout == 60, "login 命令可用（方案二恢复登录）")

    # report 命令（report.bat 路径）重出报表时也要带卖家画像标注
    root = TMP / "cli_report"
    report_cfg = replace(CFG, root=root, watches=[CFG.watches[0]])
    report_cfg.ensure_dirs()
    conn = connect(report_cfg.db_path)
    try:
        scan_id = start_scan(conn, 1)
        upsert(
            conn,
            Listing(
                "report-badge",
                "华硕天选4 RTX4060 16G 512G 游戏本",
                4300,
                "https://www.goofish.com/item?id=report-badge",
                seller="信誉为本",
                seller_reviews=1538,
                seller_rating=65,
            ),
            neutral_analysis(),
            scan_id,
        )
        finish_scan(conn, scan_id, pages=1, fetched=1, kept=1)
    finally:
        conn.close()
    from types import SimpleNamespace

    with patch("xianyu.cli.Config.load", new=staticmethod(lambda _path: report_cfg)):
        with contextlib.redirect_stdout(io.StringIO()):
            code = cmd_report(
                SimpleNamespace(config="ignored.yaml", demo=False, scan=None, open=False)
            )
    check(code == 0, "report 命令正常退出")
    html_text = (report_cfg.reports_dir / "latest.html").read_text(encoding="utf-8")
    check(
        "疑似职业卖家：1538 条评价" in html_text,
        "report 命令重出报表仍带疑似职业卖家标注",
    )


def test_config_empty_lists() -> None:
    print("config empty lists")
    root = TMP / "config_empty"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "config.yaml"
    path.write_text(
        """
watches:
  - name: 测试
    keyword: 游戏本
filters:
  seller_exclude: []
signals:
  boot_reject: []
  problem_reject: []
category:
  exclude: []
review:
  port: 8899
""",
        encoding="utf-8",
    )
    cfg = Config.load(path)
    check(cfg.filters.seller_exclude == [], "显式 seller_exclude: [] 表示关闭，不回退默认词")
    check(
        cfg.signals.boot_reject == [] and cfg.signals.problem_reject == [],
        "显式空 reject 词表不被默认值顶掉",
    )
    check(cfg.category.exclude == [], "显式空品类表表示关闭")
    check(len(cfg.signals.merchant_reject) > 0, "没写的词表仍吃默认值")
    check(cfg.review.port == 8899, "review.port 可配置")
    check(Config.load(ROOT / "config.yaml").review.port == 8765, "默认审核页端口 8765")


def test_cli_mark() -> None:
    print("cli mark")
    from types import SimpleNamespace

    cfg = replace(CFG, root=TMP / "cli_mark")
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    try:
        scan_id = start_scan(conn, 1)
        upsert(
            conn,
            Listing(
                "mark-1",
                "联想拯救者R9000P RTX3060 16G 512G 游戏本",
                3300,
                "https://www.goofish.com/item?id=mark-1",
            ),
            neutral_analysis(),
            scan_id,
        )
        finish_scan(conn, scan_id, pages=1, fetched=1, kept=1)
    finally:
        conn.close()

    def run_mark(status: str) -> int:
        with patch("xianyu.cli.Config.load", new=staticmethod(lambda _path: cfg)):
            with contextlib.redirect_stdout(io.StringIO()):
                return cmd_mark(
                    SimpleNamespace(
                        config="ignored.yaml", item_id="mark-1", status=status, note=None
                    )
                )

    check(run_mark("ignored") == 0, "mark ignored 正常退出")
    conn = connect(cfg.db_path)
    try:
        row = conn.execute(
            "SELECT status, note FROM listings WHERE item_id='mark-1'"
        ).fetchone()
        reject = conn.execute(
            "SELECT reason FROM scan_rejects WHERE item_id='mark-1'"
        ).fetchone()
        check(
            row["status"] == "ignored" and row["note"] == MANUAL_REJECT_REASON,
            "CLI 忽略写入状态与人工审核备注",
        )
        check(
            reject is not None and reject["reason"] == MANUAL_REJECT_REASON,
            "CLI 忽略写进人工不符合明细",
        )
    finally:
        conn.close()

    check(run_mark("pending") == 0, "mark pending 正常退出")
    conn = connect(cfg.db_path)
    try:
        row = conn.execute(
            "SELECT status, note FROM listings WHERE item_id='mark-1'"
        ).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) FROM scan_rejects WHERE item_id='mark-1'"
        ).fetchone()[0]
        check(
            row["status"] == "pending" and row["note"] == "" and count == 0,
            "CLI 恢复 pending 清空备注并撤回不符合记录",
        )
    finally:
        conn.close()


def test_console() -> None:
    print("console")
    check(_console_command("  START  ") == "start", "控制台命令忽略大小写和首尾空格")
    check(_console_command("Report\n") == "report", "控制台命令去掉换行")

    root = TMP / "console"
    cfg = replace(CFG, root=root)
    with contextlib.redirect_stdout(io.StringIO()):
        check(not _open_latest_report(cfg), "没有报表时 report 不打开")
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "latest.html").write_text("<html>ok</html>", encoding="utf-8")
    opened: list[str] = []
    with patch("webbrowser.open", lambda url: opened.append(url) or True):
        with contextlib.redirect_stdout(io.StringIO()):
            check(_open_latest_report(cfg), "有报表时 report 打开成功")
    check(bool(opened) and opened[0].startswith("file:"), "report 用浏览器打开本地 HTML")


class _FakeCrawler:
    """测试用假浏览器：记录 start/stop 次数，模拟请求计数。"""

    instances = 0

    def __init__(self, cfg, *, headless=None, logger=print):
        type(self).instances += 1
        self.stats = {"search_requests": 0}
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    @property
    def request_total(self) -> int:
        return int(self.stats.get("search_requests", 0))


def _fake_round_factory(rounds: list[int]):
    from types import SimpleNamespace

    def fake_round(cfg, watches, **kwargs):
        stop_event = kwargs["stop_event"]
        crawler = kwargs.get("crawler")
        if crawler is not None:
            crawler.stats["search_requests"] += 2
        rounds.append(1)
        return SimpleNamespace(
            scan_id=len(rounds),
            stats={"fetched": 0},
            errors=[],
            paths={},
            items=[],
            stopped=stop_event.is_set(),
        )

    return fake_round


def _wait(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_loop_controller() -> None:
    print("loop controller")
    rounds: list[int] = []
    cfg = replace(
        CFG,
        root=TMP / "loop_ctrl",
        loop=replace(CFG.loop, min_round_gap_seconds=0),
    )

    args = build_parser().parse_args(["loop"])
    controller = _LoopController(cfg, args)
    _FakeCrawler.instances = 0
    with patch("xianyu.cli.run_round", _fake_round_factory(rounds)), patch(
        "xianyu.cli.Crawler", _FakeCrawler
    ):
        with contextlib.redirect_stdout(io.StringIO()):
            controller.start()
            check(_wait(lambda: len(rounds) == 1), "start 启动第一轮")
            check(_wait(lambda: not controller.busy()), "第一轮结束后回到等待")
            check(
                _wait(lambda: controller._last_round_end is not None),
                "第一轮结束后记录结束时间",
            )
            check(len(rounds) == 1, "纯手动：不输入 next 不会自动跑下一轮")
            controller.next_round()
            check(_wait(lambda: len(rounds) == 2), "输入 next 才开始下一轮")
            check(_wait(lambda: not controller.busy()), "第二轮结束回到等待")
            controller.stop()
            if controller._thread is not None:
                controller._thread.join(3)
    check(not controller.running(), "stop 后后台循环退出")
    check(_FakeCrawler.instances == 1, "跨轮只启动一个浏览器（复用）")
    check(controller._crawler is None, "循环结束后释放浏览器")


def test_loop_gap() -> None:
    print("loop gap")
    rounds: list[int] = []
    cfg = replace(
        CFG,
        root=TMP / "loop_gap",
        loop=replace(CFG.loop, min_round_gap_seconds=300),
    )
    args = build_parser().parse_args(["loop"])
    controller = _LoopController(cfg, args)
    output = io.StringIO()
    _FakeCrawler.instances = 0
    with patch("xianyu.cli.run_round", _fake_round_factory(rounds)), patch(
        "xianyu.cli.Crawler", _FakeCrawler
    ):
        with contextlib.redirect_stdout(output):
            controller.start()
            check(_wait(lambda: len(rounds) == 1), "第一轮启动")
            check(
                _wait(lambda: controller._last_round_end is not None),
                "第一轮结束并记录结束时间",
            )
            controller.next_round()
            time.sleep(0.4)
            check(len(rounds) == 1, "距上一轮太近时，第一次 next 不放行")
            check(
                "再输入一次 next" in output.getvalue(),
                "提前 next 会提示需要再确认一次",
            )
            controller.next_round()
            check(_wait(lambda: len(rounds) == 2), "再次 next 才真正开始下一轮")
            controller.stop()
            if controller._thread is not None:
                controller._thread.join(3)
    check(not controller.running(), "停止后会话退出")


def test_loop_budget_stop() -> None:
    print("loop budget stop")
    rounds: list[int] = []
    cfg = replace(
        CFG,
        root=TMP / "loop_budget",
        loop=replace(CFG.loop, min_round_gap_seconds=0),
        scan=replace(CFG.scan, daily_request_budget=2),
    )

    def fake_round(cfg, watches, **kwargs):
        crawler = kwargs.get("crawler")
        if crawler is not None:
            crawler.stats["search_requests"] += 2
        DailyBudget(budget_path(cfg.data_dir), cfg.scan.daily_request_budget).add(2)
        rounds.append(1)
        from types import SimpleNamespace

        return SimpleNamespace(
            scan_id=len(rounds),
            stats={"fetched": 0},
            errors=[],
            paths={},
            items=[],
            stopped=False,
        )

    args = build_parser().parse_args(["loop"])
    controller = _LoopController(cfg, args)
    _FakeCrawler.instances = 0
    with patch("xianyu.cli.run_round", fake_round), patch(
        "xianyu.cli.Crawler", _FakeCrawler
    ):
        with contextlib.redirect_stdout(io.StringIO()):
            controller.start()
            check(_wait(lambda: not controller.running(), 4.0), "预算用尽后循环自行停止")
            if controller._thread is not None:
                controller._thread.join(3)
    check(len(rounds) == 1, "达到每日请求预算后不再跑下一轮")
    check(
        DailyBudget(budget_path(cfg.data_dir), 2).used == 2,
        "循环停止后预算落盘保留用量",
    )


def test_loop_failure_stops() -> None:
    """单轮异常直接停，不自动重建浏览器再试一次。"""
    print("loop failure")
    calls: list[int] = []

    def boom(cfg, watches, **kwargs):
        calls.append(1)
        raise RuntimeError("模拟抓取异常")

    args = build_parser().parse_args(["loop"])
    controller = _LoopController(CFG, args)
    _FakeCrawler.instances = 0
    with patch("xianyu.cli.run_round", boom), patch("xianyu.cli.Crawler", _FakeCrawler):
        with contextlib.redirect_stdout(io.StringIO()):
            controller.start()
            check(_wait(lambda: not controller.running(), 4.0), "异常后循环自行停止")
            if controller._thread is not None:
                controller._thread.join(3)
    check(len(calls) == 1, "单轮异常不自动重试")
    check(controller._crawler is None, "异常后释放浏览器")


def test_loop_push() -> None:
    print("loop push")
    from types import SimpleNamespace

    pushes: list[tuple[bool, bool]] = []

    def fake_round(cfg, watches, **kwargs):
        return SimpleNamespace(
            scan_id=1,
            stats={"fetched": 0},
            errors=[],
            paths={},
            items=[],
            stopped=False,
        )

    def fake_push(cfg, *, demo, no_notify):
        pushes.append((demo, no_notify))

    cfg = replace(
        CFG,
        root=TMP / "loop_push",
        loop=replace(CFG.loop, min_round_gap_seconds=0),
    )
    args = build_parser().parse_args(["loop", "--rounds", "2", "--no-notify"])
    controller = _LoopController(cfg, args)
    with patch("xianyu.cli.run_round", fake_round), patch(
        "xianyu.cli._push_if_configured", fake_push
    ), patch("xianyu.cli.Crawler", _FakeCrawler):
        with contextlib.redirect_stdout(io.StringIO()):
            controller.start()
            check(_wait(lambda: len(pushes) == 1), "第一轮结束后推送")
            check(
                _wait(lambda: controller._last_round_end is not None),
                "第一轮结束并进入等待",
            )
            controller.next_round()
            check(_wait(lambda: not controller.running(), 4.0), "跑满轮数后循环退出")
            if controller._thread is not None:
                controller._thread.join(3)
    check(
        pushes == [(False, True), (False, True)],
        "循环每轮结束后都尝试推送，并透传 --no-notify",
    )


def test_push_queue() -> None:
    print("push queue")
    cfg = replace(
        CFG,
        root=TMP / "push",
        notify=NotifyConfig(wecom_webhook="https://example.invalid/wecom"),
    )
    conn = connect(cfg.db_path)
    scan_id = start_scan(conn, 1)
    for item_id in ("p1", "p2"):
        upsert(
            conn,
            Listing(item_id, "联想拯救者R9000P RTX3060 16G 512G 游戏本", 3300, "u"),
            neutral_analysis(),
            scan_id,
        )
    conn.commit()
    conn.close()

    calls: list[set[str]] = []

    def failing(cfg, items, title="", *, sender=None):
        calls.append({str(item["item_id"]) for item in items})
        return NotifyResult(messages=["推送失败"], sent_ids=set())

    with patch("xianyu.notify.push", failing):
        with contextlib.redirect_stdout(io.StringIO()):
            _push_if_configured(cfg, demo=False, no_notify=False)
            _push_if_configured(cfg, demo=False, no_notify=False)
    check(calls == [{"p1", "p2"}, {"p1", "p2"}], "推送失败的商品下一轮继续重试")

    def succeeding(cfg, items, title="", *, sender=None):
        calls.append({str(item["item_id"]) for item in items})
        return NotifyResult(
            messages=["推送成功"],
            sent_ids={str(item["item_id"]) for item in items},
        )

    calls.clear()
    with patch("xianyu.notify.push", succeeding):
        with contextlib.redirect_stdout(io.StringIO()):
            _push_if_configured(cfg, demo=False, no_notify=False)
            _push_if_configured(cfg, demo=False, no_notify=False)
    check(calls == [{"p1", "p2"}], "推送成功后的商品不再重复推送")

    with patch("xianyu.notify.push", failing):
        with contextlib.redirect_stdout(io.StringIO()):
            _push_if_configured(cfg, demo=False, no_notify=True)
    check(len(calls) == 1, "--no-notify 不进入推送队列")


def test_notify() -> None:
    print("notify")
    ok, _ = _check_response('{"errcode":0,"errmsg":"ok"}')
    check(ok, "企业微信 errcode=0 判定成功")
    ok, detail = _check_response('{"errcode":93000,"errmsg":"invalid webhook url"}')
    check(not ok and "93000" in detail, "企业微信 errcode 非 0 判定失败")
    ok, _ = _check_response('{"code":200,"message":"success"}')
    check(ok, "Bark code=200 判定成功")
    ok, _ = _check_response("<html>proxy error</html>")
    check(not ok, "非 JSON 响应判定失败")
    ok, _ = _check_response('{"foo":"bar"}')
    check(not ok, "缺少状态字段判定失败")

    quiet = push(replace(CFG, notify=NotifyConfig()), [], sender=lambda *_: (True, "ok"))
    check(quiet.messages == [] and quiet.sent_ids == set(), "未配置推送地址时完全静默")

    sent: list[dict] = []
    cfg = replace(CFG, notify=NotifyConfig(wecom_webhook="https://example.invalid/wecom"))
    result = push(
        cfg,
        [row_to_item(report_item(), int(time.time() * 1000))],
        sender=lambda _url, payload: (sent.append(payload), (True, "ok"))[1],
    )
    content = sent[0]["markdown"]["content"]
    check(
        "¥3,300" in content and "参考" not in content and "0 分" not in content,
        "评分关闭时推送只显示价格，不显示参考价/0 分",
    )
    check(result.sent_ids == {"1001"}, "推送成功记录商品 ID")


# ------------------------------------------------------------------- login / 低频
def test_login_helpers() -> None:
    print("login helpers")
    root = TMP / "login"
    cfg = replace(CFG, root=root)
    check(
        login_state_path(cfg) == root / "data" / "login_state.json",
        "登录标记放在 data 目录",
    )
    check(not is_logged_in(cfg), "没有登录标记时视为未登录")

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    login_state_path(cfg).write_text(
        json.dumps({"unb": "123456", "login_at": "2026-10-22 10:00:00"}),
        encoding="utf-8",
    )
    check(is_logged_in(cfg), "有 unb 的登录标记视为已登录")

    login_state_path(cfg).write_text(json.dumps({"unb": "0"}), encoding="utf-8")
    check(not is_logged_in(cfg), "unb=0 视为未登录")

    login_state_path(cfg).write_text(json.dumps({"unb": " undefined "}), encoding="utf-8")
    check(not is_logged_in(cfg), "unb=undefined/空白 视为未登录")

    login_state_path(cfg).write_text(json.dumps({"unb": "null"}), encoding="utf-8")
    check(not is_logged_in(cfg), "unb=null 视为未登录")

    class FakeContext:
        def __init__(self, cookies):
            self._cookies = cookies

        def cookies(self):
            return self._cookies

    names = login_cookie_names(
        FakeContext(
            [
                {"name": "unb", "value": "0"},
                {"name": "sn", "value": "null"},
                {"name": "uc1", "value": "undefined"},
                {"name": "lgc", "value": "real-value"},
                {"name": "t", "value": "匿名也有，不算"},
            ]
        )
    )
    check(names == {"lgc"}, "登录 cookie 的 0/null/undefined 合并判为匿名")
    check(
        login_cookie_names(FakeContext([{"name": "unb", "value": "123456"}]))
        == {"unb"},
        "有值的登录 cookie 正常计入",
    )

    login_state_path(cfg).write_text("not json", encoding="utf-8")
    check(not is_logged_in(cfg), "损坏的登录标记不炸，视为未登录")

    args = build_parser().parse_args(["login"])
    check(args.command == "login", "login 子命令注册成功")


def test_budget() -> None:
    print("budget")
    path = budget_path(TMP / "budget")
    budget = DailyBudget(path, 10)
    check(budget.used == 0 and not budget.exhausted(), "新预算从 0 开始")
    budget.add(4)
    check(not budget.exhausted() and budget.used == 4, "预算内不停止")
    check(DailyBudget(path, 10).used == 4, "预算落盘：新实例读回已用请求数")
    budget.add(6)
    check(budget.exhausted() and budget.remaining() == 0, "达到预算后停止")

    budget.day = "2000-01-01"
    budget.add(1)
    check(budget.used == 1 and not budget.exhausted(), "跨天自动重置预算")
    check(DailyBudget(path, 10).used == 1, "跨天重置结果也落盘")

    unlimited = DailyBudget(budget_path(TMP / "budget_unlimited"), 0)
    unlimited.add(9999)
    check(
        not unlimited.exhausted() and unlimited.remaining() is None,
        "预算 0 表示不限制",
    )


# --------------------------------------------------------------------------- stealth
def test_stealth() -> None:
    print("stealth")
    from xianyu.stealth import (
        apply_stealth,
        bypass_count,
        load_bypasses,
        stealth_ignore_default_args,
        stealth_launch_args,
    )

    scripts = load_bypasses()
    check(bypass_count() == 5, "5 条 bypass 脚本（screen_props 实测有害已移除）")
    check(len(scripts) == 5, "脚本全部加载")
    check(
        not any("windowScreenProps" in s for s in scripts),
        "不注入 screen_props（会篡改真实窗口值，见 stealth.py 实测结论）",
    )
    check(scripts[-1].find("__pwInitScripts") > 0, "最后一条清理 Playwright 痕迹")
    check(any("webdriver" in s for s in scripts), "包含 webdriver 伪装")
    check(any("plugins" in s for s in scripts), "包含插件列表伪装")

    class FakeContext:
        def __init__(self):
            self.injected: list[str] = []

        def add_init_script(self, script=None, path=None):
            self.injected.append(script or path)

    ctx = FakeContext()
    count = apply_stealth(CFG, ctx)
    check(count == 5 and len(ctx.injected) == 5, "默认注入 5 条")
    check(ctx.injected == list(scripts), "注入顺序与上游一致")

    off = replace(CFG, browser=replace(CFG.browser, stealth=False))
    ctx_off = FakeContext()
    check(
        apply_stealth(off, ctx_off) == 0 and ctx_off.injected == [],
        "browser.stealth=false 时不注入",
    )

    args = stealth_launch_args()
    check("--incognito" not in args, "不带 --incognito（与长期 profile 冲突）")
    check(
        not any(a.startswith("--lang") or a.startswith("--accept-lang") for a in args),
        "不强改语言（中文场景）",
    )
    ignored = stealth_ignore_default_args()
    check("--enable-automation" in ignored, "清理 --enable-automation")
    check(len(ignored) == 5, "清理 5 个有害默认参数")

    check(CFG.browser.stealth is True, "config.yaml 默认开启 stealth")


def main() -> int:
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    tests = [
        test_parse,
        test_coarse,
        test_stealth,
        test_fine,
        test_crawler_edges,
        test_store,
        test_scan_labeling,
        test_guard,
        test_scan_lock,
        test_selectors_centralised,
        test_parse_fixtures,
        test_rate_limited_routing,
        test_sort_and_early_stop,
        test_newest_filter_click,
        test_search_loop_offline,
        test_page_state_and_baxia,
        test_report,
        test_seller_and_problem_words,
        test_report_ignore_button,
        test_price_range_filter,
        test_reject_persistence,
        test_review_page,
        test_engine,
        test_real_demo,
        test_real_1day,
        test_real_newest,
        test_rescreen,
        test_freshness,
        test_review_signals_and_order,
        test_engine_budget,
        test_pricing_retained,
        test_login_helpers,
        test_budget,
        test_cli,
        test_config_empty_lists,
        test_cli_mark,
        test_console,
        test_loop_controller,
        test_loop_gap,
        test_loop_budget_stop,
        test_loop_failure_stops,
        test_loop_push,
        test_push_queue,
        test_notify,
    ]
    try:
        for test in tests:
            test()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print()
    print(f"全部通过：{PASSED} 项")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"\n✗ 自测失败：{exc}")
        raise SystemExit(1)
