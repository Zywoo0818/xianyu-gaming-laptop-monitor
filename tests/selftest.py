"""离线自测：解析 / 显卡 / 品牌 / 品类闸门 / 行情表 / 打分 / 入库 / 报表。"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from xianyu import brands, gpus  # noqa: E402
from xianyu.config import Config, ConfigError  # noqa: E402
from xianyu.models import Listing  # noqa: E402
from xianyu.parse import parse_item, parse_payload  # noqa: E402
from xianyu.pricetable import PriceRow, load_table, match_reference, normalize_name  # noqa: E402
from xianyu.pricing import REJECT_BLOCKED_TAILS, analyze, gate, passes_filters  # noqa: E402
from xianyu.report import render_html, row_to_item  # noqa: E402
from xianyu.review import _page  # noqa: E402
from xianyu.store import (  # noqa: E402
    connect,
    ignore_pending,
    rows_for_scan,
    rows_ignored,
    set_status,
    start_scan,
    upsert,
)
from xianyu.textmatch import hit_words  # noqa: E402

PASSED = 0
CFG = Config.load(ROOT / "config.yaml")

# 固定行情夹具：自测不依赖主人重新导入的 xlsx，行情表内容变化不会让测试变红。
FIXTURE_ROWS = [
    PriceRow(name="拯救者R9000P", gpu="3060", price=3650, norm=normalize_name("拯救者R9000P")),
    PriceRow(name="拯救者R7000P", gpu="3060", price=3400, norm=normalize_name("拯救者R7000P")),
    PriceRow(name="天选4", gpu="4060", price=5000, norm=normalize_name("天选4")),
    PriceRow(name="战神Z8", gpu="2060", price=1800, norm=normalize_name("战神Z8")),
    PriceRow(name="战神Z7", gpu="1060", price=1200, norm=normalize_name("战神Z7")),
]


def check(condition: bool, message: str) -> None:
    global PASSED
    if not condition:
        raise AssertionError(message)
    PASSED += 1
    print(f"  ✓ {message}")


def sample_wrapper(item_id="1001", title="联想拯救者R9000P 2021 RTX3060 16G", price="3300"):
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
                    },
                    "clickParam": {"args": {"publishTime": str(int(time.time() * 1000)), "wantNum": "12"}},
                }
            }
        }
    }


def test_parse() -> None:
    print("parse")
    listing = parse_item(sample_wrapper())
    check(listing is not None, "解析出商品")
    assert listing
    check(listing.price == 3300, "价格解析正确")
    check(listing.url == "https://www.goofish.com/item?id=1001", "链接由 ID 生成")
    items, _ = parse_payload({"ret": ["SUCCESS::调用成功"], "data": {"resultList": [sample_wrapper("2", "B", "99"), {"bad": 1}]}})
    check(len(items) == 1, "跳过无法识别的条目")


def test_gpu() -> None:
    print("gpu")
    check(gpus.detect("RTX 3060 游戏本").model == "3060", "识别 3060")
    check(gpus.detect("rtx4060 独显").series == 40, "识别 4060")
    check(gpus.detect("GTX 1660Ti").model == "1660Ti", "识别 1660Ti")
    check(gpus.detect("1050ti 显卡").score < -20, "1050Ti 大幅减分")
    check(gpus.detect("RTX A2000").is_workstation, "专业卡识别为工作站")
    check(gpus.detect("RX 6600M").is_nvidia is False, "A 卡不是 N 卡")
    check(gpus.detect("i7 11800H 16G") is None, "没有显卡信息")
    check(gpus.detect("RTX 3060Ti").score >= 14, "3060Ti 不应低于 3060")
    check(gpus.detect("RTX 4060Ti").score >= 15, "4060Ti 不应低于 4060")
    check(gpus.detect("GTX 1650Ti").score < 0, "1650Ti 不应加分")
    check(gpus.detect("RTX 3080 Super").score == 7, "Super 变体回退到基础型号分数")
    check(gpus.detect("游戏本 15.6寸 1080P 高刷 FHD") is None, "1080P 分辨率不再当成 GTX1080")
    check(gpus.detect("神舟 1920x1080 电竞屏") is None, "1920x1080 里的 1080 不算显卡")
    check(gpus.detect("神舟GTX1080 8G 游戏本").model == "1080", "真 1080 显卡照常识别")
    check(gpus.detect("1080P 屏 RTX3060 游戏本").model == "3060", "分辨率后面还有显卡时取显卡")


def test_brands() -> None:
    print("brands")
    check(brands.detect("MacBook Pro 14").excluded, "苹果排除")
    check(brands.detect("华为 MateBook 14").excluded, "华为排除")
    check(brands.detect("联想拯救者 R9000P").weight >= 6, "联想优先")
    check(brands.detect("机械革命蛟龙16K").weight >= 6, "机械革命优先")
    check(brands.detect("惠普暗影精灵8").weight == 0, "惠普一般")


def test_gate() -> None:
    print("gate")
    ok = gate(Listing("1", "联想拯救者R9000P RTX3060 游戏本", 3300, "u"), CFG)
    check(ok.ok, "游戏本 + N 卡通过")
    thin = gate(Listing("2", "联想小新 Air14 轻薄本 办公本", 1800, "u"), CFG)
    check(not thin.ok, "轻薄本排除")
    apple = gate(Listing("3", "MacBook Pro 14 M1 Pro", 4200, "u"), CFG)
    check(not apple.ok, "苹果排除")
    ws = gate(Listing("4", "移动工作站 RTX A2000 图形卡", 3800, "u"), CFG)
    check(not ws.ok, "工作站排除")
    nogpu = gate(Listing("5", "游戏本 16G 512G 高配", 3000, "u"), CFG)
    check(not nogpu.ok, "没有 N 卡排除")
    mixed = gate(Listing("6", "游戏本 工作站 RTX3060 16G", 3000, "u"), CFG)
    check(not mixed.ok, "「游戏本 + 工作站」混合标题不再放行")
    fake_desktop = gate(Listing("7", "游戏本 RTX3060 台式机 主机", 3000, "u"), CFG)
    check(not fake_desktop.ok, "「游戏本 + 台式机」混合标题不再放行")

    no_disassembly = gate(
        Listing("8", "联想拯救者R9000P RTX3060 16G 无拆机 原装配件齐全 个人自用", 3000, "u"), CFG
    )
    check(no_disassembly.ok, "「无拆机 原装配件齐全」不再被硬排除")
    gifts = gate(
        Listing("9", "联想拯救者R9000P RTX3060 16G 带原装电源适配器 送鼠标键盘", 3000, "u"), CFG
    )
    check(gifts.ok, "「送鼠标键盘 / 带原装电源适配器」按赠品放行")
    contrast = gate(
        Listing(
            "10",
            "联想拯救者R9000P RTX3060 16G 可外接显示器 比台式机便携 三风扇散热",
            3000,
            "u",
        ),
        CFG,
    )
    check(contrast.ok, "「比台式机便携 / 可外接显示器 / 三风扇」不再误杀")

    bag = gate(Listing("11", "联想拯救者R9000P RTX3060 电脑包 内胆包 双肩包 全新", 3000, "u"), CFG)
    check(not bag.ok, "电脑包/内胆包被排除")
    film = gate(Listing("12", "联想拯救者R9000P RTX3060 键盘膜 保护壳 贴纸", 3000, "u"), CFG)
    check(not film.ok, "键盘膜/保护壳被排除")
    stand = gate(Listing("13", "拯救者R9000P RTX3060 鼠标垫 散热底座 支架", 3000, "u"), CFG)
    check(not stand.ok, "鼠标垫/散热底座被排除")
    charger = gate(Listing("14", "230W 电源适配器 原装 9成新 RTX3060", 3000, "u"), CFG)
    check(not charger.ok, "没有整机上下文的电源适配器被排除")
    parts = gate(Listing("15", "联想拯救者R9000P RTX3060 当配件出 主板好的", 3000, "u"), CFG)
    check(not parts.ok, "「当配件出」被排除")


def test_fatal_reject() -> None:
    print("fatal reject")
    watch = CFG.watches[0]

    def check_fatal(title: str, desc: str = "", seller: str = "") -> None:
        listing = Listing("f", title, 3300, "u", seller=seller, desc=desc)
        result = analyze(listing, watch, FIXTURE_ROWS, CFG)
        check(result.excluded and result.fatal, f"硬排除：{title[:34]}")

    def check_keep(title: str) -> None:
        result = analyze(Listing("f", title, 3300, "u"), watch, FIXTURE_ROWS, CFG)
        check(not result.excluded and not result.fatal, f"保留：{title[:40]}")

    check_fatal("联想拯救者R9000P RTX3060 空机出售")
    check_fatal("联想拯救者R9000P RTX3060 不含内存")
    check_fatal("联想拯救者R9000P RTX3060 不含硬盘")
    check_fatal("联想拯救者R9000P RTX3060 内存硬盘都没有")
    check_fatal("联想拯救者R9000P RTX3060 4G内存 512G固态")
    check_fatal("联想拯救者R9000P RTX3060 16G内存 64G固态")

    check_keep("联想拯救者R9000P RTX3060 8G内存 512G固态")
    check_keep("联想拯救者R9000P RTX3060 16G内存 120G固态")
    check_keep("联想拯救者R9000P RTX3060 16G内存 128G固态")
    check_keep("联想拯救者R9000P RTX3060 16G内存 512G固态")
    check_keep("联想拯救者R9000P RTX3060 16G内存 1T固态")
    check_keep("联想拯救者R9000P RTX3060 4G*2 512G")
    check_keep("联想拯救者R9000P RTX3060 128G SSD + 1T HDD")

    check_keep("联想拯救者R9000P RTX3060 16G内存 无硬盘位")
    check_keep("联想拯救者R9000P RTX3060 16G内存 无内存插槽")
    check_keep("联想拯救者R9000P RTX3060 最大支持 32G 内存")
    check_fatal("联想拯救者R9000P RTX3060 4G内存 可升级到 16G")

    check_fatal("联想拯救者R9000P RTX3060 无法开机")
    check_fatal("联想拯救者R9000P RTX3060 开不了机")
    check_fatal("联想拯救者R9000P RTX3060 不能开机")
    check_fatal("联想拯救者R9000P RTX3060 不开机")
    check_fatal("联想拯救者R9000P RTX3060 点不亮")
    check_keep("联想拯救者R9000P RTX3060 黑屏 会修的来")

    check_fatal("机械革命旷世 游戏本 RTX4060 16G 1T", seller="草莓精品二手数码")
    check_fatal("华强北数码城 出 联想拯救者R9000P RTX3060 16G")
    check_keep("联想拯救者R9000P RTX3060 16G 不是店铺 个人自用")

    check_fatal("联想拯救者R9000P RTX3060 16G 512G", desc="只有空机，没有内存硬盘")
    check_fatal("联想拯救者R9000P RTX3060 16G 512G", desc="机器无法开机")
    check_keep("联想拯救者R9000P RTX3060 16G 512G 不是空机 无拆修")

    check(
        hit_words("无硬盘位", CFG.signals.merchant_reject, blocked_tails=REJECT_BLOCKED_TAILS)
        == [],
        "「无硬盘位」不再命中「无硬盘」",
    )
    check(
        "无硬盘"
        in hit_words("无硬盘 空机", CFG.signals.merchant_reject, blocked_tails=REJECT_BLOCKED_TAILS),
        "真正的「无硬盘」仍会命中",
    )


def test_filters() -> None:
    print("filters")
    watch = CFG.watches[0]
    normal = Listing("10", "联想拯救者R9000P RTX3060 16G", 3300, "u", seller="小陈同学")
    check(passes_filters(normal, watch, CFG), "普通个人卖家通过")
    shop = Listing("11", "联想拯救者R9000P RTX3060 16G", 3300, "u", seller="华强北数码城")
    check(passes_filters(shop, watch, CFG), "商家昵称不在初筛阶段静默丢弃")
    shop_gate = gate(shop, CFG)
    check(shop_gate.fatal and "商家" in shop_gate.reason, "卖家昵称含 seller_exclude 词被硬排除")
    recycled = Listing("12", "联想拯救者R9000P RTX3060 16G", 3300, "u", seller="专业回收工作室")
    check(gate(recycled, CFG).fatal, "回收工作室昵称被硬排除")
    clean = Listing(
        "13", "联想拯救者R9000P RTX3060 16G 无拆机 原装配件齐全", 3300, "u", seller="小陈同学"
    )
    check(passes_filters(clean, watch, CFG), "「无拆机」不再被 filters.exclude 误杀")
    recycle = Listing("14", "高价回收 联想拯救者R9000P RTX3060", 3300, "u", seller="小陈同学")
    check(not passes_filters(recycle, watch, CFG), "「高价回收」仍被过滤")


def test_reference_tiers() -> None:
    print("reference / 档位")
    price, name, source = match_reference("联想拯救者R9000P 2021 RTX3060Ti 16G", "3060Ti", FIXTURE_ROWS)
    check(price == 3650 and source == "table/model", f"3060Ti 回退到 3060 档并命中型号：{price} {name}")

    # 同一台机器多个显卡版本：型号名撞到了，但档位必须一致，不能拿 2060 的价当 1650Ti 的参考价
    price, name, source = match_reference("神舟战神Z8 GTX1650Ti 16G 游戏本", "1650Ti", FIXTURE_ROWS)
    check(price == 1200 and source == "table/proxy", f"1650Ti 不跨档撞 2060 型号价，按 1060 估算：{price}")

    price, _, source = match_reference("游戏本 RTX3050 16G 512G", "3050", FIXTURE_ROWS)
    check(source == "table/proxy" and abs(price - 3525 * 0.85) < 1, f"3050 按 3060 中位价估算：{price}")

    price, _, source = match_reference("游戏本 RTX4080 16G", "4080", FIXTURE_ROWS)
    check(source == "table/proxy" and price > 5000, f"4080 按 4060 档估算：{price}")

    price, _, source = match_reference("游戏本 RTX2050 16G", "2050", FIXTURE_ROWS)
    check(source == "table/proxy", f"2050 也有参考价：{price}")

    price, _, source = match_reference("游戏本 RX6600M 16G", "AMD", FIXTURE_ROWS)
    check(price is None, "认不出的档位宁可没有参考价，也不跨档瞎给")


def test_negation() -> None:
    print("negation")
    broken = CFG.signals.broken
    check(hit_words("功能正常 无第三方维修", broken) == [], "「无第三方维修」不再算故障")
    check("维修" in hit_words("无拆修，维修过", broken), "分隔符挡窗口：「无拆修，维修过」仍算命中")
    check("维修" in hit_words("进水 主板维修", broken), "真故障照常命中")
    check(hit_words("无维修 无暗病 无拆修", broken) == [], "无XX 三连全部当成否定")
    check(
        hit_words("无拆机 原装配件齐全 个人自用", broken) == [],
        "「原装配件齐全」不再算故障/配件描述",
    )

    now = int(time.time() * 1000)
    clean = Listing("21", "联想拯救者R9000P RTX3060 16G 无第三方维修 功能正常", 3300, "u")
    clean.publish_ts = now
    result = analyze(clean, CFG.watches[0], FIXTURE_ROWS, CFG, now)
    check(not result.suspicious, f"「无第三方维修」不再被压进异常区：{result.score} 分")

    complete = Listing("22", "联想拯救者R9000P RTX3060 16G 无拆机 原装配件齐全 个人自用", 3300, "u")
    complete.publish_ts = now
    complete_result = analyze(complete, CFG.watches[0], FIXTURE_ROWS, CFG, now)
    check(
        not complete_result.excluded and not complete_result.suspicious,
        f"「无拆机 原装配件齐全」正常进入打分：{complete_result.score} 分",
    )


def test_config_guard() -> None:
    print("config")
    check(bool(CFG.signals.merchant_reject), "merchant_reject 硬排除词表生效")
    check(bool(CFG.signals.boot_reject), "boot_reject 开机故障词表生效")
    check(bool(CFG.signals.merchant), "merchant 商家硬排除词表生效")
    check("数码" in CFG.filters.seller_exclude, "seller_exclude 包含数码类商家词")
    check(CFG.scan.detail_limit == 20, "scan.detail_limit 可读")

    tmp = ROOT / "data" / "_selftest"
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / "bad_watch.yaml"
    path.write_text(
        "watches:\n"
        "  - name: t\n"
        "    keyword: 游戏本 3060\n"
        "    specs: [{pattern: 16G, add: 5}]\n",
        encoding="utf-8",
    )
    try:
        Config.load(path)
        check(False, "watch 里的无效键应该报错")
    except ConfigError as exc:
        check("specs" in str(exc), "watch 里写了不生效的键会直接报错，不再静默忽略")
    path.unlink()


def test_table_and_score() -> None:
    print("table / score")
    table_path = ROOT / "data" / "_selftest_table.json"
    table_path.parent.mkdir(parents=True, exist_ok=True)
    table_path.write_text(
        json.dumps(
            {"rows": [{"name": "拯救者R9000P", "gpu": "3060", "price": 3650}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    try:
        loaded = load_table(table_path)
        check(len(loaded) == 1 and loaded[0].price == 3650, "行情表可读：1 条")
    finally:
        table_path.unlink()
    price, name, source = match_reference("联想拯救者R9000P 2021 RTX3060 16G", "3060", FIXTURE_ROWS)
    check(price == 3650 and source.startswith("table"), f"匹配到 R9000P：{price} {name}")

    now = int(time.time() * 1000)
    listing = Listing("1", "联想拯救者R9000P 2021 RTX3060 16G 512G 个人自用 无拆修", 3300, "u")
    listing.publish_ts = now - 30 * 60 * 1000
    result = analyze(listing, CFG.watches[0], FIXTURE_ROWS, CFG, now)
    check(not result.excluded, "通过闸门")
    check(result.score >= 60, f"低价得高分：{result.score}")
    check(result.gpu == "3060" and result.brand == "联想", "记录显卡与品牌")

    tight = Listing("2", "联想拯救者R9000P 2021 RTX3060", 3600, "u")
    tight.publish_ts = now
    check(analyze(tight, CFG.watches[0], FIXTURE_ROWS, CFG, now).excluded, "价差不足 100 直接排除")

    bait = Listing("3", "联想拯救者R9000P RTX3060 游戏本 定金 500 先到先得", 2000, "u")
    bait.publish_ts = now
    check(analyze(bait, CFG.watches[0], FIXTURE_ROWS, CFG, now).suspicious, "定金商品标记异常")

    gpu_bonus = Listing("4", "神舟战神Z8 RTX2060 16G", 1500, "u")
    gpu_bonus.publish_ts = now
    check(analyze(gpu_bonus, CFG.watches[0], FIXTURE_ROWS, CFG, now).gpu_score >= 10, "2060 重点加分")


def test_store_and_report() -> None:
    print("store / report")
    tmp = ROOT / "data" / "_selftest"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        db = tmp / "test.db"
        conn = connect(db)
        scan_id = start_scan(conn, 1)
        watch = CFG.watches[0]
        listing = Listing("999", "联想拯救者R9000P 2021 RTX3060 16G", 3300, "https://www.goofish.com/item?id=999")
        listing.watch = watch.name
        result = analyze(listing, watch, FIXTURE_ROWS, CFG)
        check(upsert(conn, listing, result, scan_id), "首次入库标记为新")
        check(not upsert(conn, listing, result, scan_id), "再次入库不是新")
        rows = rows_for_scan(conn, scan_id)
        check(len(rows) == 1, "能按扫描批次取出")
        item = row_to_item(rows[0], int(time.time() * 1000))
        html = render_html([item], title="t", subtitle="s", stats={"fetched": 1, "kept": 1, "watches": 1}, generated_at="now")
        check("https://www.goofish.com/item?id=999" in html, "报表包含链接")
        check("¥3,300" in html, "报表包含价格")
        check("3060" in html, "报表包含显卡")

        second = Listing("1000", "联想拯救者R9000P 2021 RTX3060 16G 512G", 3300, "u")
        second.watch = watch.name
        second_result = analyze(second, watch, FIXTURE_ROWS, CFG)
        check(upsert(conn, second, second_result, scan_id), "第二条商品入库")
        check(set_status(conn, "999", "ignored"), "能手动标记为已忽略")
        check(
            ignore_pending(conn, {"999": "人工已处理", "1000": "疑似商家：空机出售"}) == 1,
            "自动忽略只改 pending，不覆盖人工状态",
        )
        row = conn.execute("SELECT status, note FROM listings WHERE item_id='1000'").fetchone()
        check(
            row["status"] == "ignored" and row["note"].startswith("自动忽略："),
            "自动忽略会写入原因",
        )
        check(rows_for_scan(conn, scan_id) == [], "已忽略商品不出现在扫描数据里")
        check(len(rows_ignored(conn)) == 2, "已忽略板块能取到全部条目")
        conn.close()

        page = _page(CFG, db)
        check("已忽略" in page, "审核页包含已忽略板块")
        check("恢复待审核" in page, "审核页支持恢复待审核")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_review_page() -> None:
    print("review page")
    tmp = ROOT / "data" / "_selftest_review"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        db = tmp / "review.db"
        conn = connect(db)
        scan_id = start_scan(conn, 1)
        watch = CFG.watches[0]
        now = int(time.time() * 1000)
        cases = [
            ("2001", "联想拯救者R9000P RTX3060 16G 512G 高分样本", 2000),
            ("2002", "联想拯救者R9000P RTX3060 16G 512G 低分样本", 3400),
            ("2003", "联想拯救者R9000P RTX3060 16G 512G 定金 异常样本", 2000),
        ]
        for item_id, title, price in cases:
            listing = Listing(item_id, title, price, "u", seller="小陈同学")
            listing.watch = watch.name
            upsert(conn, listing, analyze(listing, watch, FIXTURE_ROWS, CFG, now), scan_id)
        conn.commit()
        conn.close()

        page = _page(CFG, db)
        check('data-tab="suspicious"' not in page, "审核页不再有异常低价分类")
        check(
            'data-tab="pending"' in page and 'data-tab="ignored"' in page,
            "审核页只保留待审核和已忽略",
        )
        check('id="q"' not in page and 'data-f="top"' not in page, "审核页移除筛选工具栏")
        check("异常低价" in page, "异常商品显示异常低价标签")
        check(
            page.index("高分样本") < page.index("低分样本") < page.index("异常样本"),
            "待审核按分数从高到低排序",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print(f"配置：{CFG.path}")
    test_parse()
    test_gpu()
    test_brands()
    test_gate()
    test_fatal_reject()
    test_filters()
    test_reference_tiers()
    test_negation()
    test_config_guard()
    test_table_and_score()
    test_store_and_report()
    test_review_page()
    print(f"\n全部通过：{PASSED} 项检查")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\n✗ 失败：{exc}")
        sys.exit(1)
