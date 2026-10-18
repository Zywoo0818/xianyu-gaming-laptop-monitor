"""离线自测：解析 / 显卡 / 品牌 / 品类闸门 / 行情表 / 打分 / 入库 / 报表。"""
from __future__ import annotations

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
from xianyu.config import Config  # noqa: E402
from xianyu.models import Listing  # noqa: E402
from xianyu.parse import parse_item, parse_payload  # noqa: E402
from xianyu.pricetable import load_table, match_reference  # noqa: E402
from xianyu.pricing import analyze, gate  # noqa: E402
from xianyu.report import render_html, row_to_item  # noqa: E402
from xianyu.store import connect, rows_for_scan, start_scan, upsert  # noqa: E402

PASSED = 0
CFG = Config.load(ROOT / "config.yaml")
ROWS = load_table(CFG.price_table.path)


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


def test_table_and_score() -> None:
    print("table / score")
    check(len(ROWS) >= 30, f"行情表已导入：{len(ROWS)} 条")
    price, name, source = match_reference("联想拯救者R9000P 2021 RTX3060 16G", "3060", ROWS)
    check(price == 3650 and source.startswith("table"), f"匹配到 R9000P：{price} {name}")

    now = int(time.time() * 1000)
    listing = Listing("1", "联想拯救者R9000P 2021 RTX3060 16G 512G 个人自用 无拆修", 3300, "u")
    listing.publish_ts = now - 30 * 60 * 1000
    result = analyze(listing, CFG.watches[0], ROWS, CFG, now)
    check(not result.excluded, "通过闸门")
    check(result.score >= 60, f"低价得高分：{result.score}")
    check(result.gpu == "3060" and result.brand == "联想", "记录显卡与品牌")

    tight = Listing("2", "联想拯救者R9000P 2021 RTX3060", 3600, "u")
    tight.publish_ts = now
    check(analyze(tight, CFG.watches[0], ROWS, CFG, now).excluded, "价差不足 100 直接排除")

    bait = Listing("3", "联想拯救者R9000P RTX3060 游戏本 定金 500 先到先得", 2000, "u")
    bait.publish_ts = now
    check(analyze(bait, CFG.watches[0], ROWS, CFG, now).suspicious, "定金商品标记异常")

    gpu_bonus = Listing("4", "神舟战神Z8 RTX2060 16G", 1500, "u")
    gpu_bonus.publish_ts = now
    check(analyze(gpu_bonus, CFG.watches[0], ROWS, CFG, now).gpu_score >= 10, "2060 重点加分")


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
        result = analyze(listing, watch, ROWS, CFG)
        check(upsert(conn, listing, result, scan_id, {"merchant": False}), "首次入库标记为新")
        check(not upsert(conn, listing, result, scan_id), "再次入库不是新")
        rows = rows_for_scan(conn, scan_id)
        check(len(rows) == 1, "能按扫描批次取出")
        item = row_to_item(rows[0], int(time.time() * 1000))
        html = render_html([item], title="t", subtitle="s", stats={"fetched": 1, "kept": 1, "watches": 1}, generated_at="now")
        check("https://www.goofish.com/item?id=999" in html, "报表包含链接")
        check("¥3,300" in html, "报表包含价格")
        check("3060" in html, "报表包含显卡")
        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print(f"配置：{CFG.path}")
    test_parse()
    test_gpu()
    test_brands()
    test_gate()
    test_table_and_score()
    test_store_and_report()
    print(f"\n全部通过：{PASSED} 项检查")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\n✗ 失败：{exc}")
        sys.exit(1)
