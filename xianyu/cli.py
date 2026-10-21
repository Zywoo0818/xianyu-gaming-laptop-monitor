"""命令行入口：login / scan / report / review / list / mark / stats / doctor。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from .config import Config, ConfigError
from .crawler import do_login, is_logged_in
from .engine import ScanPaused, run_scan
from .lock import ScanBusy
from .report import money, row_to_item, write_all
from . import __version__
from .store import (
    STATUS_LABELS,
    connect,
    count_by_status,
    get_scan,
    latest_scan,
    list_rows_by_status,
    mark_notified,
    pending_notify,
    rows_for_scan,
    set_status,
)

BANNER = "闲鱼笔记本 · 低价筛选"


def _say(message: str = "") -> None:
    print(message, flush=True)


# --------------------------------------------------------------------------- login
def cmd_login(args) -> int:
    cfg = Config.load(args.config)
    cfg.ensure_dirs()
    _say(f"登录态目录：{cfg.browser.profile_dir}")
    _say("即将打开 Chrome，请用闲鱼 App 扫码。")
    try:
        payload = do_login(cfg, timeout_seconds=args.timeout, logger=_say)
    except Exception as exc:  # noqa: BLE001
        _say(f"✗ 登录失败：{exc}")
        return 1
    _say(f"✓ 已保存登录态：{payload['login_at']}")
    if is_logged_in(cfg):
        _say(f"✓ 已确认登录 cookie（用户 ID {payload['unb']}），本次没有发起搜索请求。")
    _say("  首次扫描请手动运行：scan.bat")
    return 0


# ---------------------------------------------------------------------------- scan
def cmd_scan(args) -> int:
    cfg = Config.load(args.config)
    watches = [cfg.watch(name) for name in args.watch] if args.watch else cfg.watches
    if not args.demo and not is_logged_in(cfg):
        _say("⚠ 还没检测到登录态。若抓不到数据，先运行：python -m xianyu login")
    _say(f"=== {BANNER} ===")
    _say(f"关键词 {len(watches)} 个 · 每个 {args.pages or cfg.scan.pages} 页 · 模式：{'演示' if args.demo else '真实抓取'}")

    _say("")
    headless = False if args.headed else None
    db_path = (cfg.data_dir / "demo.db") if args.demo else None
    try:
        outcome = run_scan(
            cfg,
            watches,
            headless=headless,
            demo=args.demo,
            pages=args.pages,
            db_path=db_path,
            force=args.force,
            logger=_say,
        )
    except ScanPaused as exc:
        _say("")
        _say(f"⚠ {exc}")
        return 0
    except ScanBusy as exc:
        _say("")
        _say(f"⚠ {exc}，本轮跳过")
        return 0
    except ConfigError as exc:
        _say(f"✗ 配置错误：{exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        _say(f"✗ 抓取失败：{type(exc).__name__}: {exc}")
        _say("  如果提示 WinError 5 / 管道错误，请手动双击 scan.bat 再跑一次。")
        return 1

    _say("")
    _say(f"扫描 #{outcome.scan_id} 完成，用时 {outcome.elapsed:.1f}s")
    _say(f"抓取 {outcome.stats['fetched']} 条 → 入选 {outcome.stats['kept']} 条（新增 {outcome.new_count} 条）")
    if outcome.errors:
        _say("")
        for message in outcome.errors:
            _say(f"  ✗ {message}")

    top = outcome.recommended[: args.top]
    if top:
        _say("")
        _say("— 最值得看的几条 —")
        for index, item in enumerate(top, start=1):
            _say(
                f"{index}. [{item['score']:.0f} 分] {money(item['price'])} "
                f"（参考 {money(item['ref_price'])}，低 {item['discount']:.0%}）"
            )
            _say(f"   {item['title'][:60]}")
            _say(f"   {item['url']}")
    else:
        _say("")
        _say("本轮没有达到分数的商品。可以调低 scan.min_score，或放宽 scan.min_gap。")

    _say("")
    _say(f"报表：{outcome.paths['html']}")
    _say(f"最新：{outcome.paths['latest_html']}")
    _say(f"摘要：{outcome.paths['md']}")

    if args.demo and cfg.notify.enabled and not args.no_notify:
        _say("  演示模式：不发送真实推送")

    if not args.no_notify and not args.demo and cfg.notify.enabled:
        from .notify import push

        conn = connect(db_path or cfg.db_path)
        try:
            now_ms = int(datetime.now().timestamp() * 1000)
            queue = pending_notify(conn, cfg.notify.min_score)
            result = push(
                cfg,
                [row_to_item(row, now_ms) for row in queue],
                title="闲鱼低价提醒",
            )
            if result.sent_ids:
                mark_notified(conn, result.sent_ids)
        finally:
            conn.close()
        for message in result.messages:
            _say(f"  {message}")
        if result.sent_ids:
            _say(f"  已记录 {len(result.sent_ids)} 条为已推送，下次不会重复")

    if args.open_report and outcome.paths.get("latest_html"):
        import webbrowser

        webbrowser.open(outcome.paths["latest_html"].resolve().as_uri())
    return 0


# -------------------------------------------------------------------------- report
def cmd_report(args) -> int:
    cfg = Config.load(args.config)
    conn = connect((cfg.data_dir / "demo.db") if args.demo else cfg.db_path)
    scan = get_scan(conn, args.scan) if args.scan else latest_scan(conn)
    if scan is None:
        _say("还没有扫描记录，先运行：python -m xianyu scan")
        return 1
    rows = rows_for_scan(conn, int(scan["id"]))
    now_ms = int(datetime.now().timestamp() * 1000)
    items = [row_to_item(row, now_ms) for row in rows]
    for item in items:
        item["is_new"] = item.get("first_seen") == item.get("last_seen")
    conn.close()
    stats = {
        "fetched": scan["fetched"] or 0,
        "kept": len(items),
        "watches": scan["watches"] or 0,
        "elapsed": 0,
        "top_n": cfg.scan.top_n_markdown,
    }
    paths = write_all(
        cfg.reports_dir,
        items,
        title=f"{BANNER} · 扫描 #{scan['id']}",
        subtitle=f"{scan['started_at']} · 抓到 {stats['fetched']} 条 · 入选 {stats['kept']} 条",
        stats=stats,
    )
    _say(f"已生成报表：{paths['html']}")
    if args.open:
        import webbrowser

        webbrowser.open(paths["latest_html"].resolve().as_uri())
    return 0


# -------------------------------------------------------------------------- review
def cmd_review(args) -> int:
    cfg = Config.load(args.config)
    from .review import serve

    db_path = (cfg.data_dir / "demo.db") if args.demo else None
    try:
        serve(cfg, port=args.port, open_browser=not args.no_open, db_path=db_path)
    except OSError as exc:
        _say(f"✗ 端口 {args.port} 启动失败：{exc}")
        _say("  换一个端口：python -m xianyu review --port 8766")
        return 1
    return 0


# ---------------------------------------------------------------------------- list
def cmd_list(args) -> int:
    cfg = Config.load(args.config)
    conn = connect((cfg.data_dir / "demo.db") if args.demo else cfg.db_path)
    rows = list_rows_by_status(conn, None if args.status == "all" else args.status, args.limit)
    conn.close()
    if not rows:
        _say("没有符合条件的商品。")
        return 0
    _say(f"共 {len(rows)} 条：")
    for index, row in enumerate(rows, start=1):
        status = STATUS_LABELS.get(row["status"], row["status"])
        flag = " ⚠" if row["suspicious"] else ""
        _say(
            f"{index:>2}. [{row['score']:.0f}] {money(row['price'])} "
            f"参考 {money(row['ref_price'])} · {status}{flag} · {row['title'][:48]}"
        )
        _say(f"    {row['url']}")
    return 0


# ---------------------------------------------------------------------------- mark
def cmd_mark(args) -> int:
    if args.status not in STATUS_LABELS:
        _say(f"✗ 状态只能是：{', '.join(STATUS_LABELS)}")
        return 2
    cfg = Config.load(args.config)
    conn = connect(cfg.db_path)
    row = conn.execute("SELECT item_id FROM listings WHERE item_id=?", (args.item_id,)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT item_id FROM listings WHERE item_id LIKE ? ORDER BY score DESC LIMIT 1",
            (f"%{args.item_id}",),
        ).fetchone()
    if row is None:
        conn.close()
        _say("✗ 没找到这个商品 ID")
        return 1
    set_status(conn, row["item_id"], args.status, args.note)
    conn.close()
    _say(f"✓ 已标记为「{STATUS_LABELS[args.status]}」：{row['item_id']}")
    return 0


# --------------------------------------------------------------------------- stats
def cmd_import_prices(args) -> int:
    from .pricetable import build_table, save_table

    cfg = Config.load(args.config)
    source = Path(args.xlsx) if args.xlsx else Path(cfg.price_table.source_xlsx)
    if not source.is_file():
        _say(f"✗ 找不到行情表：{source}")
        _say("  用法：python -m xianyu import-prices \"笔电行情.xlsx\"")
        return 1
    rows = build_table(source)
    if not rows:
        _say("✗ 没解析出任何有效行，检查表头（名称/CPU/GPU/内存/硬盘/电池/价格）")
        return 1
    save_table(rows, cfg.price_table.path, str(source))
    by_gpu: dict[str, list[float]] = {}
    for row in rows:
        by_gpu.setdefault(row.gpu, []).append(row.price)
    _say(f"✓ 已导入 {len(rows)} 条行情 → {cfg.price_table.path}")
    import statistics

    for gpu, prices in sorted(by_gpu.items(), key=lambda pair: -len(pair[1])):
        _say(f"  {gpu:<7} {len(prices):>2} 条  中位价 ¥{statistics.median(prices):,.0f}")
    return 0


def cmd_stats(args) -> int:
    from .pricetable import load_table

    cfg = Config.load(args.config)
    conn = connect(cfg.db_path)
    counts = count_by_status(conn)
    scan = latest_scan(conn)
    conn.close()

    rows = load_table(cfg.price_table.path) if cfg.price_table else []
    _say(f"行情表：{len(rows)} 条（{cfg.price_table.path}）")
    by_gpu: dict[str, list[float]] = {}
    for row in rows:
        by_gpu.setdefault(row.gpu, []).append(row.price)
    import statistics

    for gpu, prices in sorted(by_gpu.items(), key=lambda pair: -len(pair[1])):
        _say(f"  {gpu:<7} {len(prices):>2} 条  中位价 ¥{statistics.median(prices):,.0f}")
    if scan is not None:
        _say("")
        _say(f"最近一次扫描 #{scan['id']}：{scan['started_at']} · 抓取 {scan['fetched']} · 入选 {scan['kept']}")
    _say("")
    _say("审核状态：" + " · ".join(f"{STATUS_LABELS.get(key, key)} {value}" for key, value in counts.items()))
    return 0


# -------------------------------------------------------------------------- doctor
def cmd_doctor(args) -> int:
    ok = True
    _say(f"=== {BANNER} · 环境自检 ===")
    _say(f"Python：{sys.version.split()[0]}  ({sys.executable})")
    if sys.version_info < (3, 10):
        _say("✗ 需要 Python 3.10 以上")
        ok = False

    for module in ("yaml", "playwright"):
        try:
            __import__(module)
            _say(f"✓ 依赖 {module}")
        except ImportError:
            _say(f"✗ 缺少依赖 {module}，运行：pip install -r requirements.txt")
            ok = False

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401

        _say("✓ Playwright 可用")
    except Exception as exc:  # noqa: BLE001
        _say(f"✗ Playwright 异常：{exc}")
        ok = False

    chrome = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    found = next((path for path in chrome if Path(path).is_file()), None) or shutil.which("chrome")
    if found:
        _say(f"✓ 浏览器：{found}")
    else:
        _say("⚠ 没找到 Chrome，请在 config.yaml 里把 browser.channel 改成 null，并安装 Playwright Chromium")

    try:
        cfg = Config.load(args.config)
        _say(f"✓ 配置：{cfg.path}（{len(cfg.watches)} 个关键词）")
    except ConfigError as exc:
        _say(f"✗ 配置错误：{exc}")
        return 2

    cfg.ensure_dirs()
    table_rows = 0
    if cfg.price_table and cfg.price_table.path.is_file():
        try:
            from .pricetable import load_table

            table_rows = len(load_table(cfg.price_table.path))
        except Exception:  # noqa: BLE001
            table_rows = 0
    if table_rows:
        _say(f"✓ 行情表：{table_rows} 条（{cfg.price_table.path}）")
    else:
        _say(f"⚠ 行情表为空：{cfg.price_table.path if cfg.price_table else '未配置'}")
        _say("  运行：python -m xianyu import-prices \"E:\\useful\\文件\\笔电行情.xlsx\"")
    _say(f"{'✓' if is_logged_in(cfg) else '⚠'} 登录态：{cfg.data_dir / 'login_state.json'}"
         + ("" if is_logged_in(cfg) else "（还没登录，运行 python -m xianyu login）"))
    from .guard import RiskGuard

    _say(f"· {RiskGuard(cfg).describe()}")
    _say(f"{'✓' if cfg.db_path.is_file() else '·'} 数据库：{cfg.db_path}")
    try:
        probe = cfg.reports_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        _say(f"✓ 报表目录可写：{cfg.reports_dir}")
    except OSError as exc:
        _say(f"✗ 报表目录不可写：{exc}")
        ok = False
    _say("")
    _say("自检完成。" if ok else "自检发现问题，按上面的提示处理后再试。")
    return 0 if ok else 1


# ---------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")

    parser = argparse.ArgumentParser(prog="python -m xianyu", description=BANNER)
    parser.add_argument("--version", action="version", version=f"xianyu-monitor {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_login = sub.add_parser("login", parents=[common], help="扫码登录并保存登录态")
    p_login.add_argument("--timeout", type=int, default=300, help="等待扫码的秒数（默认 300）")
    p_login.set_defaults(func=cmd_login)

    p_scan = sub.add_parser("scan", parents=[common], help="抓取 + 筛选 + 出报表")
    p_scan.add_argument("--watch", action="append", help="只跑指定监控项，可重复")
    p_scan.add_argument("--pages", type=int, help="覆盖每个关键词的抓取页数")
    p_scan.add_argument("--headed", action="store_true", help="显示浏览器窗口（风控时用）")
    p_scan.add_argument("--demo", action="store_true", help="用内置演示数据，不联网")
    p_scan.add_argument("--no-notify", action="store_true", help="本次不推送")
    p_scan.add_argument("--force", action="store_true", help="忽略风控冷却，强制扫描一次")
    p_scan.add_argument("--open-report", action="store_true", help="结束后自动打开报表")
    p_scan.add_argument("--top", type=int, default=5, help="终端里打印前几条（默认 5）")
    p_scan.set_defaults(func=cmd_scan)

    p_report = sub.add_parser("report", parents=[common], help="用数据库里最近一次结果重新出报表")
    p_report.add_argument("--scan", type=int, help="指定扫描编号")
    p_report.add_argument("--demo", action="store_true", help="读取演示数据库")
    p_report.add_argument("--open", action="store_true", help="生成后打开")
    p_report.set_defaults(func=cmd_report)

    p_review = sub.add_parser("review", parents=[common], help="打开本地人工审核页")
    p_review.add_argument("--port", type=int, default=8765)
    p_review.add_argument("--demo", action="store_true", help="读取演示数据库")
    p_review.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    p_review.set_defaults(func=cmd_review)

    p_list = sub.add_parser("list", parents=[common], help="在终端里列出商品")
    p_list.add_argument("--status", default="pending", choices=[*STATUS_LABELS, "all"])
    p_list.add_argument("--limit", type=int, default=30)
    p_list.add_argument("--demo", action="store_true", help="读取演示数据库")
    p_list.set_defaults(func=cmd_list)

    p_mark = sub.add_parser("mark", parents=[common], help="标记某条商品的状态")
    p_mark.add_argument("item_id", help="商品 ID（可只写后几位）")
    p_mark.add_argument("status", choices=list(STATUS_LABELS))
    p_mark.add_argument("--note", help="备注")
    p_mark.set_defaults(func=cmd_mark)

    p_stats = sub.add_parser("stats", parents=[common], help="查看行情表与审核统计")
    p_stats.add_argument("--days", type=int, default=3, help="（保留参数）")
    p_stats.set_defaults(func=cmd_stats)

    p_prices = sub.add_parser("import-prices", parents=[common], help="导入/更新笔电行情 xlsx")
    p_prices.add_argument("xlsx", nargs="?", help="xlsx 路径，默认用 config 里的 source_xlsx")
    p_prices.set_defaults(func=cmd_import_prices)

    p_doctor = sub.add_parser("doctor", parents=[common], help="环境自检")
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        _say("")
        _say("第一次使用：")
        _say("  1) python -m xianyu login      扫码登录")
        _say("  2) python -m xianyu scan       抓取并出报表")
        _say("  3) python -m xianyu review     打开审核页")
        return 0
    try:
        return int(args.func(args))
    except ConfigError as exc:
        _say(f"✗ 配置错误：{exc}")
        return 2
    except KeyboardInterrupt:
        _say("\n已取消。")
        return 130
