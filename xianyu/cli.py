"""命令行入口：login / scan / loop / report / review / list / mark / stats / doctor。

方案二（登录 + 只搜搜索页 + 手动 loop + 低频）：
- login：有头浏览器扫码登录，登录态落在长期复用的 browser profile 里；
- scan / loop：只抓搜索页，粗筛（搜索页商家判断）之后直接出报表，
  不打开任何卖家主页——跨商品商家识别转人工审核；
- loop：纯手动，每轮结束输入 next 才继续，没有任何自动续跑。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from .budget import DailyBudget, budget_path
from .config import Config, ConfigError
from .crawler import Crawler, do_login, is_logged_in, login_state_path
from .engine import ScanPaused, run_round
from .guard import is_login_error, is_risk_error
from .lock import ScanBusy, ScanLock
from .report import (
    ignored_row_to_reject_item,
    is_pro_seller,
    money,
    reject_row_to_item,
    row_to_item,
    write_all,
)
from .rescreen import apply_rescreen, plan_rescreen
from .scanlog import log_scan
from . import __version__
from .store import (
    MANUAL_REJECT_REASON,
    STATUS_LABELS,
    connect,
    count_by_status,
    get_scan,
    latest_scan,
    list_rows_by_status,
    load_seller_checks,
    mark_manual_reject,
    mark_notified,
    pending_notify,
    rows_pending,
    rows_rejects,
    rows_manual_rejects,
    restore_pending,
    set_status,
)

BANNER = "闲鱼游戏本 · 方案二 登录低频 · 只搜搜索页"
_PRINT_LOCK = threading.Lock()


def _say(message: str = "") -> None:
    with _PRINT_LOCK:
        print(message, flush=True)


def _watches(cfg: Config, names: list[str] | None):
    return [cfg.watch(name) for name in names] if names else cfg.watches


def _console_command(line: str) -> str:
    return str(line or "").strip().casefold()


def _open_latest_report(cfg: Config) -> bool:
    path = cfg.reports_dir / "latest.html"
    if not path.is_file():
        _say("还没有报表；先输入 start 跑一轮。")
        return False
    _say(f"打开报表：{path}")
    import webbrowser

    webbrowser.open(path.resolve().as_uri())
    return True


def _round_summary(outcome) -> None:
    stats = outcome.stats
    _say("")
    _say(
        f"本轮 #{outcome.scan_id}：搜索 {stats.get('fetched', 0)} 条 → "
        f"粗筛通过 {stats.get('coarse', 0)} 条 → "
        f"不符合 {stats.get('rejected', 0)} 条 → "
        f"待审核 {stats.get('kept', 0)} 条"
        f"（新增 {stats.get('new', 0)}）"
    )
    if outcome.errors:
        _say("")
        for message in outcome.errors:
            _say(f"  ✗ {message}")
    sort_text = str(stats.get("sort_status") or "")
    if sort_text:
        _say(f"  排序：{sort_text}")
    _say("")
    if outcome.stopped:
        _say("本轮已按 stop 结束，不生成新报表。")
    elif outcome.paths.get("html"):
        _say(f"报表：{outcome.paths['html']}")


def _show_pending(outcome, limit: int) -> None:
    top = outcome.items[:limit]
    if not top:
        _say("")
        _say("当前没有待审核商品。")
        return
    _say("")
    _say("— 待审核（商家识别转人工：点开链接看卖家在售数）—")
    for index, item in enumerate(top, start=1):
        mark = "[NEW] " if item.get("is_new") else ""
        _say(f"{index}. {mark}{money(item['price'])}  {item['title'][:64]}")
        reason = "；".join(item["reasons"][:2])
        if reason:
            _say(f"   {reason}")
        _say(f"   {item['url']}")
    if len(outcome.items) > limit:
        _say(f"   ……另有 {len(outcome.items) - limit} 条，见报表。")


def _push_if_configured(cfg: Config, *, demo: bool, no_notify: bool) -> None:
    if demo or no_notify or not cfg.notify.enabled:
        return
    from .notify import push

    conn = connect(cfg.db_path)
    try:
        now_ms = int(datetime.now().timestamp() * 1000)
        queue = pending_notify(conn, cfg.notify.min_score)
        if not queue:
            return
        result = push(
            cfg,
            [row_to_item(row, now_ms) for row in queue],
            title="闲鱼游戏本待审核",
        )
        if result.sent_ids:
            mark_notified(conn, result.sent_ids)
    finally:
        conn.close()
    for message in result.messages:
        _say(f"  {message}")


def _mark_scan_items(items: list[dict], scan_id: int) -> None:
    target = int(scan_id)
    for item in items:
        item["is_new"] = int(item.get("first_scan_id") or 0) == target


# --------------------------------------------------------------------------- login
def cmd_login(args) -> int:
    cfg = Config.load(args.config)
    _say(f"=== {BANNER} ===")
    if is_logged_in(cfg):
        _say(f"已有登录态（{login_state_path(cfg)}）；重新扫码会覆盖它。")
    _say("")
    try:
        payload = do_login(cfg, timeout_seconds=args.timeout, logger=_say)
    except KeyboardInterrupt:
        _say("\n已取消。")
        return 130
    except Exception as exc:  # noqa: BLE001
        _say(f"✗ 登录失败：{type(exc).__name__}: {exc}")
        return 1
    _say("")
    _say(f"✓ 已保存登录态：{payload['login_at']}")
    _say(f"✓ 已确认登录 cookie（用户 ID {payload['unb']}），本次没有发起搜索请求。")
    return 0


# ---------------------------------------------------------------------------- scan
def _scan_db_path(cfg: Config, *, demo: bool, demo_file: Path | None) -> Path | None:
    """合成演示落 demo.db 方便复看；真实回放用临时库，保证每次都是干净一轮。"""
    if demo_file is not None:
        return Path(":memory:")
    if demo:
        return cfg.data_dir / "demo.db"
    return None


def cmd_scan(args) -> int:
    cfg = Config.load(args.config)
    watches = _watches(cfg, args.watch)
    demo_file = Path(args.demo_file) if getattr(args, "demo_file", None) else None
    demo = bool(args.demo or demo_file)
    _say(f"=== {BANNER} ===")
    if not demo and cfg.browser.require_login and not is_logged_in(cfg):
        _say("⚠ 还没检测到登录态。若抓不到数据，先运行：login.bat")
        _say("")
    mode = (
        "演示（回放抓包，临时库）"
        if demo_file
        else ("演示" if demo else "登录抓取（只搜搜索页）")
    )
    _say(
        f"关键词 {'、'.join(watch.keyword for watch in watches)} · "
        f"每个 {args.pages or cfg.scan.pages} 页 · 模式：{mode}"
    )
    _say("")
    headless = False if args.headed else None
    db_path = _scan_db_path(cfg, demo=bool(args.demo), demo_file=demo_file)
    try:
        outcome = run_round(
            cfg,
            watches,
            headless=headless,
            demo=demo,
            demo_file=demo_file,
            pages=args.pages,
            db_path=db_path,
            force=args.force,
            dump=getattr(args, "dump", False),
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

    _round_summary(outcome)
    _show_pending(outcome, args.top)
    _say("")
    _say(f"最新：{outcome.paths.get('latest_html', '')}")
    _say(f"摘要：{outcome.paths.get('md', '')}")

    _push_if_configured(cfg, demo=demo, no_notify=args.no_notify)

    if args.open_report and outcome.paths.get("latest_html"):
        import webbrowser

        webbrowser.open(outcome.paths["latest_html"].resolve().as_uri())
    return 0


# ---------------------------------------------------------------------------- loop
class _LoopController:
    """后台跑轮次，主线程接收 start / next / stop / report / exit。

    方案二特性：
    - 纯手动：每轮结束后必须输入 next 才跑下一轮，没有任何自动续跑；
    - 相邻两轮有最小间隔，提前输入 next 需要再确认一次；
    - 跨轮复用同一个浏览器（loop.reuse_browser），不再每轮重启；
    - 每日请求预算落盘累计，用完后自动停（scan 与 loop 共用）；
    - 风控/登录失效/单轮异常即停循环，不自动重试。
    """

    def __init__(self, cfg: Config, args):
        self.cfg = cfg
        self.args = args
        self.watches = _watches(cfg, args.watch)
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._next_event = threading.Event()
        self._lock = threading.Lock()
        self._busy = False
        self._last_round_end: float | None = None
        self._crawler: Crawler | None = None

    def running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def start(self) -> None:
        if self.running():
            if self.busy():
                _say("本轮仍在运行；需要停止请输入 stop。")
            else:
                _say("循环会话已在进行：输入 next 开始下一轮，stop 结束会话。")
            return
        stop_event = threading.Event()
        self._next_event.clear()
        with self._lock:
            self._stop_event = stop_event
            self._thread = threading.Thread(
                target=self._run, args=(stop_event,), name="xianyu-loop", daemon=True
            )
            self._thread.start()
        _say("已开始循环会话。输入 next 跑下一轮，stop 结束，report 打开报表。")

    def next_round(self) -> None:
        if not self.running():
            _say("还没有开始循环；先输入 start。")
            return
        if self.busy():
            _say("本轮仍在运行，请稍候。")
            return
        self._next_event.set()

    def stop(self) -> None:
        with self._lock:
            stop_event = self._stop_event
            running = bool(self._thread and self._thread.is_alive())
            busy = self._busy
        if not running or stop_event is None:
            _say("当前没有运行中的循环。")
            return
        stop_event.set()
        self._next_event.set()
        if busy:
            _say("已发送 stop：不再发起新请求；正在进行的单个请求会自然结束，本轮不生成新报表。")
        else:
            _say("已结束循环会话。")

    def interrupt(self) -> None:
        with self._lock:
            stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()
            self._next_event.set()

    # ------------------------------------------------------------------ internals
    def _ensure_crawler(self) -> Crawler | None:
        if self._crawler is not None:
            return self._crawler
        if not self.cfg.loop.reuse_browser:
            return None
        crawler = Crawler(self.cfg, logger=_say)
        try:
            crawler.start()
        except Exception as exc:  # noqa: BLE001 - 启动失败给出可操作提示
            _say(f"✗ 浏览器启动失败：{type(exc).__name__}: {exc}")
            _say("  请确认已安装 Google Chrome，或把 browser.channel 改成 null 并安装 Playwright Chromium。")
            return None
        self._crawler = crawler
        return crawler

    def _drop_crawler(self) -> None:
        crawler, self._crawler = self._crawler, None
        if crawler is not None:
            try:
                crawler.stop()
            except Exception:  # noqa: BLE001
                pass

    def _await_next(self, stop_event: threading.Event) -> bool:
        """等用户输入 next；返回 False 表示会话结束。

        相邻两轮有最小间隔；提前输入 next 只提示，再输入一次才放行。
        """
        self._next_event.clear()
        gap = max(0, int(self.cfg.loop.min_round_gap_seconds))
        warned = False
        _say("（输入 next 开始下一轮，stop 结束会话）")
        while not stop_event.is_set():
            if not self._next_event.wait(0.5):
                continue
            self._next_event.clear()
            if stop_event.is_set():
                return False
            if gap and self._last_round_end is not None and not warned:
                elapsed = time.monotonic() - self._last_round_end
                if elapsed < gap:
                    remaining = max(1, int(gap - elapsed + 0.999))
                    _say(
                        f"  ⚠ 距上一轮结束仅 {int(elapsed)} 秒，建议至少间隔 {gap} 秒；"
                        f"再等约 {remaining} 秒。"
                    )
                    _say("    确实要现在继续，请再输入一次 next。")
                    warned = True
                    continue
            return True
        return False

    def _run(self, stop_event: threading.Event) -> None:
        round_no = 0
        last_outcome = None
        try:
            while not stop_event.is_set():
                round_no += 1
                label = f"第 {round_no} 轮 {datetime.now():%H:%M:%S}"
                _say("")
                _say("=" * 64)
                _say(label)
                crawler = self._ensure_crawler()
                if crawler is None and self.cfg.loop.reuse_browser:
                    return
                with self._lock:
                    self._busy = True
                try:
                    outcome = run_round(
                        self.cfg,
                        self.watches,
                        headless=False if self.args.headed else None,
                        pages=self.args.pages,
                        force=self.args.force and round_no == 1,
                        stop_event=stop_event,
                        label=label,
                        logger=_say,
                        crawler=crawler,
                    )
                except ScanPaused as exc:
                    _say("")
                    _say(f"⚠ {exc}")
                    _say("循环停止：等冷却结束或手动处理后，再输入 start。")
                    return
                except ScanBusy as exc:
                    _say("")
                    _say(f"⚠ {exc}，循环停止。")
                    return
                except ConfigError as exc:
                    _say(f"✗ 配置错误：{exc}")
                    return
                except Exception as exc:  # noqa: BLE001
                    _say(f"✗ 第 {round_no} 轮失败：{type(exc).__name__}: {exc}")
                    _say("循环停止：不自动重试。处理后重新输入 start。")
                    self._drop_crawler()
                    return
                finally:
                    with self._lock:
                        self._busy = False

                last_outcome = outcome
                _round_summary(outcome)
                budget = DailyBudget(
                    budget_path(self.cfg.data_dir), self.cfg.scan.daily_request_budget
                )
                _say(f"  · {budget.describe()}")
                self._last_round_end = time.monotonic()
                if budget.exhausted():
                    _say("")
                    _say(f"已达每日请求预算（{budget.describe()}），循环停止。")
                    return
                if outcome.stopped or stop_event.is_set():
                    _say("")
                    _say("已停止：不再开始下一轮。")
                    return
                if any(is_login_error(message) for message in outcome.errors):
                    _say("")
                    _say("登录态失效：循环停止。运行 login.bat 重新扫码后再 start。")
                    return
                if any(is_risk_error(message) for message in outcome.errors):
                    _say("")
                    _say("检测到风控，循环停止。请在浏览器窗口里手动处理，不要连续重试。")
                    return
                _push_if_configured(
                    self.cfg,
                    demo=False,
                    no_notify=self.args.no_notify,
                )
                if self.args.rounds and round_no >= self.args.rounds:
                    _say("")
                    _say(f"已跑满 {self.args.rounds} 轮，循环结束。")
                    return
                if not self._await_next(stop_event):
                    _say("")
                    _say("已停止：不再开始下一轮。")
                    return
                _say("")
                _say("开始下一轮……")
        finally:
            self._drop_crawler()
            if (
                self.args.open_report
                and last_outcome is not None
                and not last_outcome.stopped
                and last_outcome.paths.get("html")
            ):
                import webbrowser

                webbrowser.open(last_outcome.paths["html"].resolve().as_uri())


def cmd_loop(args) -> int:
    cfg = Config.load(args.config)
    try:
        interactive = bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, ValueError):
        interactive = False
    if not interactive:
        _say("✗ loop 需要交互式控制台；计划任务请使用 scan.bat。")
        return 2

    _say(f"=== {BANNER} · 控制台 ===")
    _say("start 跑第一轮 · next 跑下一轮（纯手动，无自动续跑）· stop 停止 · report 报表 · exit 退出")
    gap = cfg.loop.min_round_gap_seconds
    gap_text = f"相邻两轮至少间隔 {gap} 秒（提前 next 需再确认）" if gap else "相邻轮次间隔不限制"
    _say(
        f"每轮 {args.pages or cfg.scan.pages} 页；翻页间隔 {cfg.browser.min_delay:g}-{cfg.browser.max_delay:g} 秒；"
        f"{gap_text}；不打开卖家主页（商家识别转人工）。"
    )
    _say("")
    controller = _LoopController(cfg, args)
    while True:
        try:
            line = _console_command(input("> "))
        except (EOFError, KeyboardInterrupt):
            _say("")
            controller.interrupt()
            _say("退出控制台。")
            break
        if not line:
            continue
        if line in {"exit", "quit"}:
            if controller.running():
                _say("循环仍在运行，先输入 stop，等收尾后再 exit。")
                continue
            break
        if line == "start":
            controller.start()
        elif line in {"next", "go", "继续"}:
            controller.next_round()
        elif line == "stop":
            controller.stop()
        elif line == "report":
            _open_latest_report(cfg)
        else:
            _say(f"未知命令：{line}")
            _say("可用：start / next / stop / report / exit")
    return 0


# -------------------------------------------------------------------------- report
def cmd_report(args) -> int:
    cfg = Config.load(args.config)
    conn = connect((cfg.data_dir / "demo.db") if args.demo else cfg.db_path)
    scan = get_scan(conn, args.scan) if args.scan else latest_scan(conn)
    if scan is None:
        conn.close()
        _say("还没有扫描记录，先运行：python -m xianyu scan")
        return 1
    rows = rows_pending(conn, 1000)
    now_ms = int(datetime.now().timestamp() * 1000)
    items = [row_to_item(row, now_ms) for row in rows]
    _mark_scan_items(items, int(scan["id"]))
    for item in items:
        item["pro_seller"] = is_pro_seller(item, cfg.scan.seller_review_hint)
    rejected = [reject_row_to_item(row, now_ms) for row in rows_rejects(conn, int(scan["id"]))]
    # 跨轮保留：人工忽略 / 积压重筛写在它们当时那一轮，新扫描后会被冲掉
    rejected += [
        ignored_row_to_reject_item(row, now_ms)
        for row in rows_manual_rejects(conn, int(scan["id"]))
    ]
    conn.close()
    stats = {
        "fetched": scan["fetched"] or 0,
        "coarse": None,
        "rejected": len(rejected),
        "kept": len(items),
        "watches": scan["watches"] or 0,
        "elapsed": 0,
        "top_n": cfg.scan.top_n_markdown,
        "sort_status": scan["sort_status"] or "",
    }
    paths = write_all(
        cfg.reports_dir,
        items,
        title=f"{BANNER} · 扫描 #{scan['id']}",
        subtitle=(
            f"{scan['started_at']} · 搜索 {stats['fetched']} 条 · "
            f"不符合 {stats['rejected']} 条 · 待审核 {stats['kept']} 条"
        ),
        stats=stats,
        rejected=rejected,
        review_port=cfg.review.port,
    )
    _say(f"已生成报表：{paths['html']}")
    if args.open:
        import webbrowser

        webbrowser.open(paths["latest_html"].resolve().as_uri())
    return 0


def cmd_rescreen(args) -> int:
    """D3 积压重筛：按现行规则重过全部待审核商品（默认 dry-run，--write 落库）。"""
    cfg = Config.load(args.config)
    cfg.ensure_dirs()
    now_ms = int(datetime.now().timestamp() * 1000)
    conn = connect(cfg.db_path)
    keep, move = plan_rescreen(conn, cfg, now_ms)
    scanned = len(keep) + len(move)
    _say(f"待审核 {scanned} 条 → 按现行规则保留 {len(keep)} / 移出 {len(move)}")
    if move:
        by_reason: dict[str, int] = {}
        for _row, reason in move:
            by_reason[reason] = by_reason.get(reason, 0) + 1
        for reason, count in sorted(by_reason.items(), key=lambda pair: -pair[1]):
            _say(f"  {count} 条 {reason}")
    if not args.write:
        _say("dry-run：没有写库。确认后加 --write 落库（会建一轮专用重筛轮次）。")
        conn.close()
        return 0
    try:
        with ScanLock(cfg.data_dir / "scan.lock"):
            stats = apply_rescreen(conn, cfg, now_ms, _say)
    except ScanBusy as exc:
        _say(f"⚠ {exc}，重筛跳过")
        conn.close()
        return 0
    conn.close()
    if stats["scan_id"] is not None:
        log_scan(
            cfg,
            status="rescreen",
            elapsed=0.0,
            outcome=SimpleNamespace(
                scan_id=stats["scan_id"],
                stats={"fetched": stats["scanned"], "kept": stats["kept"]},
            ),
        )
        _say(f"已落库（专用轮次 #{stats['scan_id']}）。重出报表：python -m xianyu report")
    return 0


# -------------------------------------------------------------------------- review
def cmd_review(args) -> int:
    cfg = Config.load(args.config)
    from .review import serve

    db_path = (cfg.data_dir / "demo.db") if args.demo else None
    port = cfg.review.port if args.port is None else args.port
    if args.port is not None and args.port != cfg.review.port:
        _say(
            f"⚠ 审核页临时改用 {args.port}：报表「忽略」键仍指向 "
            f"config.review.port={cfg.review.port}，要一致请改 config 后重出报表"
        )
    try:
        serve(cfg, port=port, open_browser=not args.no_open, db_path=db_path)
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
    now_ms = int(datetime.now().timestamp() * 1000)
    _say(f"共 {len(rows)} 条：")
    for index, row in enumerate(rows, start=1):
        item = row_to_item(row, now_ms)
        _say(
            f"{index:>2}. {money(item['price'])} · {item['status_label']} · "
            f"{item['title'][:48]}"
        )
        _say(f"    {item['url']}")
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
            "SELECT item_id FROM listings WHERE item_id LIKE ? ORDER BY first_seen DESC LIMIT 1",
            (f"%{args.item_id}",),
        ).fetchone()
    if row is None:
        conn.close()
        _say("✗ 没找到这个商品 ID")
        return 1
    if args.status == "ignored":
        # 与审核页「忽略」同一条路径：写人工不符合明细，恢复时才能一起撤回。
        mark_manual_reject(
            conn, row["item_id"], args.note or MANUAL_REJECT_REASON
        )
    elif args.status == "pending":
        restore_pending(conn, row["item_id"])
    else:
        set_status(conn, row["item_id"], args.status, args.note)
    conn.close()
    _say(f"✓ 已标记为「{STATUS_LABELS[args.status]}」：{row['item_id']}")
    if args.status == "ignored":
        _say("  已写入不符合板块（人工审核），恢复请执行：mark <ID> pending")
    return 0


# ------------------------------------------------------------------------ 行情表
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
    _say("  方案二默认 pricing.enabled=false，行情表只在使用旧比价流程时才会读取。")
    import statistics

    for gpu, prices in sorted(by_gpu.items(), key=lambda pair: -len(pair[1])):
        _say(f"  {gpu:<7} {len(prices):>2} 条  中位价 ¥{statistics.median(prices):,.0f}")
    return 0


# ---------------------------------------------------------------------------- stats
def cmd_stats(args) -> int:
    from .pricetable import load_table

    cfg = Config.load(args.config)
    conn = connect(cfg.db_path)
    counts = count_by_status(conn)
    scan = latest_scan(conn)
    conn.close()

    _say(f"登录态：{'已登录' if is_logged_in(cfg) else '未登录（运行 login.bat）'}")
    _say(f"价格筛选/评分：{'开启' if cfg.pricing.enabled else '关闭（方案二默认）'}")
    if cfg.pricing.enabled:
        rows = load_table(cfg.price_table.path) if cfg.price_table else []
        _say(f"行情表：{len(rows)} 条（{cfg.price_table.path if cfg.price_table else '未配置'}）")
    if scan is not None:
        _say("")
        _say(
            f"最近一次扫描 #{scan['id']}：{scan['started_at']} · "
            f"搜索 {scan['fetched']} · 待审核 {scan['kept']}"
        )
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
    if cfg.browser.require_login:
        if is_logged_in(cfg):
            _say(f"✓ 登录态：{login_state_path(cfg)}")
        else:
            _say("· 登录态：还没登录——双击 login.bat 扫码（搜索前需要）")
    else:
        _say("· 登录态：browser.require_login=false，按匿名抓取")
    _say(
        f"· 抓取范围：只搜搜索页（每词 {cfg.scan.pages} 页），"
        f"不打开卖家主页；翻页间隔 {cfg.browser.min_delay:g}-{cfg.browser.max_delay:g} 秒"
    )
    _say(
        f"· 搜索排序：{'进页面点「新发布 → 最新」' if cfg.scan.newest_first else '闲鱼默认（综合）'}；"
        f"发布时间窗：{f'{cfg.scan.max_age_hours:g} 小时' if cfg.scan.max_age_hours > 0 else '关'}；"
        f"老货提前停：{'开' if cfg.scan.stop_on_known else '关'}；"
        f"Baxia 人工等待 {cfg.browser.baxia_wait_seconds} 秒"
    )
    hint = cfg.scan.seller_review_hint
    _say(
        f"· 卖家画像：解析并标注（只标注不排除）；"
        f"疑似职业卖家阈值：{f'{hint} 条评价' if hint > 0 else '关'}"
    )
    budget = DailyBudget(budget_path(cfg.data_dir), cfg.scan.daily_request_budget)
    gap = cfg.loop.min_round_gap_seconds
    gap_text = (
        f"相邻两轮至少间隔 {gap} 秒（提前 next 需再确认）"
        if gap
        else "相邻轮次间隔不限制"
    )
    _say(
        f"· 节奏：纯手动（每轮输入 next）；{gap_text}；"
        f"{budget.describe()}（落盘累计，scan/loop 共用）"
    )
    _say(f"· 价格筛选/评分：{'开启' if cfg.pricing.enabled else '关闭'}")
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

    p_login = sub.add_parser("login", parents=[common], help="扫码登录，保存登录态（登录态在 browser profile 里）")
    p_login.add_argument("--timeout", type=int, default=300, help="最多等多少秒（默认 300）")
    p_login.set_defaults(func=cmd_login)

    p_scan = sub.add_parser("scan", parents=[common], help="登录抓取一轮 + 粗筛 + 出报表（不打开卖家主页）")
    p_scan.add_argument("--watch", action="append", help="只跑指定监控项，可重复")
    p_scan.add_argument("--pages", type=int, help="覆盖每个关键词的抓取页数")
    p_scan.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认就是有头）")
    p_scan.add_argument("--demo", action="store_true", help="用内置演示数据，不联网")
    p_scan.add_argument(
        "--demo-file",
        metavar="PATH",
        help="回放 --dump 保存的真实抓包（单个 JSON 或目录），不联网",
    )
    p_scan.add_argument("--no-notify", action="store_true", help="本次不推送")
    p_scan.add_argument("--force", action="store_true", help="忽略风控冷却，强制扫描一次")
    p_scan.add_argument("--dump", action="store_true", help="把接口原始响应存到 data/captures/，用于离线 fixture")
    p_scan.add_argument("--open-report", action="store_true", help="结束后自动打开报表")
    p_scan.add_argument("--top", type=int, default=10, help="终端里打印前几条（默认 10）")
    p_scan.set_defaults(func=cmd_scan)

    p_loop = sub.add_parser("loop", parents=[common], help="控制台循环：start / next / stop / report / exit")
    p_loop.add_argument("--watch", action="append", help="只跑指定监控项，可重复")
    p_loop.add_argument("--pages", type=int, help="覆盖每个关键词的抓取页数")
    p_loop.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认就是有头）")
    p_loop.add_argument("--force", action="store_true", help="第一轮忽略风控冷却")
    p_loop.add_argument("--rounds", type=int, default=0, help="最多跑几轮（0 = 不限，直到 stop/风控）")
    p_loop.add_argument("--no-notify", action="store_true", help="循环中不推送")
    p_loop.add_argument("--open-report", action="store_true", help="循环结束后打开最新报表")
    p_loop.set_defaults(func=cmd_loop)

    p_report = sub.add_parser("report", parents=[common], help="把当前全部待审核商品重新出报表")
    p_report.add_argument("--scan", type=int, help="指定扫描编号（用于标记本轮新增）")
    p_report.add_argument("--demo", action="store_true", help="读取演示数据库")
    p_report.add_argument("--open", action="store_true", help="生成后打开")
    p_report.set_defaults(func=cmd_report)

    p_rescreen = sub.add_parser(
        "rescreen",
        parents=[common],
        help="D3 积压重筛：按现行规则重过全部待审核，移出不符合的（默认 dry-run）",
    )
    p_rescreen.add_argument(
        "--write", action="store_true", help="落库：建专用重筛轮次并移出（默认只打印计划）"
    )
    p_rescreen.set_defaults(func=cmd_rescreen)

    p_review = sub.add_parser("review", parents=[common], help="打开本地人工审核页")
    p_review.add_argument(
        "--port", type=int, default=None, help="临时覆盖端口（默认取 config.review.port）"
    )
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

    p_stats = sub.add_parser("stats", parents=[common], help="查看审核统计")
    p_stats.add_argument("--days", type=int, default=3, help="（保留参数）")
    p_stats.set_defaults(func=cmd_stats)

    p_prices = sub.add_parser("import-prices", parents=[common], help="导入/更新笔电行情 xlsx（默认不启用）")
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
        _say("开始使用：")
        _say("  1) python -m xianyu scan --demo   离线看效果")
        _say("  2) login.bat                      扫码登录（只需一次）")
        _say("  3) python -m xianyu scan          登录抓取一轮（只搜搜索页）")
        _say("  4) loop.bat                       控制台（start 开始 / next 下一轮 / stop 停止）")
        _say("  5) python -m xianyu review        打开审核页")
        return 0
    try:
        return int(args.func(args))
    except ConfigError as exc:
        _say(f"✗ 配置错误：{exc}")
        return 2
    except KeyboardInterrupt:
        _say("\n已取消。")
        return 130
