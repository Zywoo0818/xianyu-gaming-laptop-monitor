"""方案二抓取层：登录态 + 有头 + 只抓搜索页，不打开卖家主页。

设计取舍（相对 v3）：
- 恢复 v2 的登录链路：login.bat 扫码 → 登录态留在长期复用的 Chrome profile 里，
  之后每轮扫描自动带着 cookie；登录 cookie 组（unb/sn/uc1/lgc）缺失时立刻停，
  绝不带着半登录态继续抓——风控后果落在账号上。
- 彻底移除卖家主页抓取：不点 personal 页、不读在售列表，
  跨商品商家识别（大疆/配件阈值）不再自动做，转人工审核。
- 低频：翻页/关键词之间 8-15 秒随机间隔；轮次完全由主人手动触发。
- 页面选择器、接口路径、风控/限流/登录/Baxia 提示语一律来自 selectors.py，本文件不写死。
- 有头 + Baxia 弹窗时停下来等主人手动滑，滑块过了本轮继续，过不了才判风控。

只抓搜索页 + 登录态 + 低频，是三种手段里请求画像最接近真人的一种；
代价是风控触发时影响的是账号本身，所以节奏必须保守。
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence, Set
from urllib.parse import quote, urlsplit

from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeout, sync_playwright

from .stealth import apply_stealth, stealth_ignore_default_args, stealth_launch_args
from . import selectors as sel
from .config import Config, Watch
from .models import Listing
from .parse import ApiRejected, ParseError, parse_payload

CAPTURE_TIMEOUT_MS = 25_000
FILTER_CAPTURE_TIMEOUT_MS = 10_000
FILTER_SETTLE_MS = 1_200
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{version} Safari/537.36"
)


def chrome_version() -> str:
    """读取本机 Chrome 版本号，用来拼一个不带 Headless 标记的 UA。"""
    roots = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application",
    ]
    found: list[tuple[int, ...]] = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", entry.name):
                    found.append(tuple(int(part) for part in entry.name.split(".")))
        except OSError:
            continue
    if not found:
        return "131.0.0.0"
    return ".".join(str(part) for part in max(found))


def default_user_agent() -> str:
    return DEFAULT_UA.format(version=chrome_version())


def prepare_browser_temp(cfg: Config) -> None:
    """把 Playwright/Chrome 的临时目录放到项目内。"""
    temp = cfg.data_dir / "browser_tmp"
    temp.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(temp)
    os.environ["TMP"] = str(temp)
    os.environ["TMPDIR"] = str(temp)


class CrawlError(Exception):
    """抓取失败的基类。"""


class LoginRequired(CrawlError):
    """需要重新扫码登录。"""


class RiskControl(CrawlError):
    """被风控拦截。"""


class RateLimited(RiskControl):
    """频率/额度超限：等一会儿就好，不该升级成整天熔断。"""


class CaptureTimeout(CrawlError):
    """页面没发出搜索请求（可能被风控或页面改版）。"""


class PaginationError(CrawlError):
    """翻页失败或超时，不能当成“已经是最后一页”。"""


def _page_number_from_request(request: Any) -> int:
    """从请求参数里解出 pageNumber，失败就当作第 1 页。"""
    values: list[str] = []
    try:
        query = urlsplit(request.url).query
    except ValueError:
        query = ""
    match = re.search(r"(?:^|&)data=([^&]+)", query)
    if match:
        values.append(match.group(1))
    post = getattr(request, "post_data", None)
    if isinstance(post, str) and post:
        match = re.search(r"(?:^|&)data=([^&]+)", post)
        if match:
            values.append(match.group(1))
        elif post.strip().startswith("{"):
            values.append(post)
    from urllib.parse import unquote_plus

    for raw in values:
        try:
            data = json.loads(unquote_plus(raw))
        except (ValueError, TypeError):
            continue
        found = _find_page_number(data)
        if found:
            return found
    return 1


def _find_page_number(value: Any, depth: int = 0) -> int | None:
    if depth > 4:
        return None
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() == "pagenumber":
                try:
                    number = int(nested)
                except (TypeError, ValueError):
                    return None
                return number if number >= 1 else None
        for nested in value.values():
            found = _find_page_number(nested, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value[:5]:
            found = _find_page_number(nested, depth + 1)
            if found:
                return found
    return None


MIN_ORDER_SAMPLE = 5


def build_search_url(keyword: str) -> str:
    """拼搜索页地址。排序不再走 URL 参数，进页面后点「新发布 → 最新」。"""
    return sel.SEARCH_URL.format(keyword=quote(keyword))


def looks_newest_first(items: Sequence[Listing]) -> bool:
    """用返回自带的 publishTime 反查这一页是不是「新→旧」。

    排序参数落不落地只认数据本身；时间戳缺失太多就当判不出来（False），
    宁可不提前停，也不能把没见过的新货漏掉。
    """
    stamps = [int(getattr(item, "publish_ts", 0) or 0) for item in items]
    stamps = [stamp for stamp in stamps if stamp > 0]
    if len(stamps) < MIN_ORDER_SAMPLE:
        return False
    pairs = list(zip(stamps, stamps[1:]))
    descending = sum(1 for earlier, later in pairs if later <= earlier)
    return descending * 5 >= len(pairs) * 4


def order_note(items: Sequence[Listing]) -> str:
    """按返回自带的 publishTime 判断这一页到底有没有按新→旧排。"""
    stamps = [int(getattr(item, "publish_ts", 0) or 0) for item in items]
    stamps = [stamp for stamp in stamps if stamp > 0]
    if len(stamps) < MIN_ORDER_SAMPLE:
        return "顺序未确认（发布时间样本不足）"
    pairs = list(zip(stamps, stamps[1:]))
    descending = sum(1 for earlier, later in pairs if later <= earlier)
    if descending * 5 >= len(pairs) * 4:
        return "顺序已确认新→旧"
    return "顺序未确认（返回不是新→旧）"


def should_stop_early(items: Sequence[Listing], known_ids: Set[str]) -> bool:
    """整页都是库里的老货，且这页确实是新→旧，才敢断定后面只会更老。"""
    if not items or not known_ids:
        return False
    if not all(item.item_id in known_ids for item in items):
        return False
    return looks_newest_first(items)


def _login_value(value: Any) -> str:
    """登录态取值：空、0、null、undefined 都是匿名态的占位值。"""
    text = str(value if value is not None else "").strip()
    return "" if text.casefold() in {"", "0", "null", "undefined"} else text


def login_cookie_names(context: BrowserContext) -> set[str]:
    """当前浏览器里存在且有值的登录 cookie 名（借鉴 second-eye：t/cookie2 匿名也有，不算）。"""
    try:
        cookies = context.cookies()
    except Exception:  # noqa: BLE001 - 浏览器刚崩时读不到 cookie
        return set()
    return {
        str(cookie["name"])
        for cookie in cookies
        if cookie.get("name") in sel.LOGIN_COOKIES and _login_value(cookie.get("value"))
    }


# ---------------------------------------------------------------------------- login
def login_state_path(cfg: Config) -> Path:
    return cfg.data_dir / "login_state.json"


def is_logged_in(cfg: Config) -> bool:
    path = login_state_path(cfg)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(_login_value(data.get("unb")))


def do_login(
    cfg: Config,
    timeout_seconds: int = 300,
    logger: Callable[[str], None] = print,
) -> dict:
    """打开有头 Chrome，等用户扫码，检测到登录后写标记文件。

    登录态（cookie）实际保存在长期复用的 browser profile 里，
    标记文件只记录用户 ID 和登录时间，供自检和过期判断。
    """
    prepare_browser_temp(cfg)
    with sync_playwright() as pw:
        options: dict[str, Any] = {
            "user_data_dir": str(cfg.browser.profile_dir),
            "headless": False,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "viewport": {"width": 1280, "height": 860},
            "args": ["--disable-blink-features=AutomationControlled", "--no-first-run"],
            "ignore_default_args": ["--enable-automation"],
        }
        if cfg.browser.channel:
            options["channel"] = cfg.browser.channel
        context = pw.chromium.launch_persistent_context(**options)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(sel.LOGIN_URL, wait_until="domcontentloaded")
        logger("")
        logger("  已打开浏览器，请用闲鱼 App 扫码登录。")
        logger(f"  登录成功后脚本会自动继续（最多等 {timeout_seconds} 秒）…")
        deadline = time.monotonic() + timeout_seconds
        unb = ""
        while time.monotonic() < deadline:
            cookies = {cookie["name"]: cookie["value"] for cookie in context.cookies()}
            unb = _login_value(cookies.get("unb", ""))
            if unb:
                break
            time.sleep(2)
        if not unb:
            context.close()
            raise LoginRequired("未检测到登录，请重试 login")
        time.sleep(1.5)
        cookies = {cookie["name"]: cookie["value"] for cookie in context.cookies()}
        payload = {
            "unb": unb,
            "login_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cookie_names": sorted(cookies.keys()),
        }
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        login_state_path(cfg).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger(f"  ✓ 登录成功（用户 ID {unb}），登录态已保存")
        time.sleep(1)
        context.close()
        return payload


class Crawler:
    """一个浏览器上下文里依次跑完所有关键词（登录态，只抓搜索页）。"""

    def __init__(
        self,
        cfg: Config,
        *,
        headless: bool | None = None,
        logger: Callable[[str], None] = print,
        dump_dir: Path | None = None,
    ):
        self.cfg = cfg
        self.headless = cfg.browser.headless if headless is None else headless
        self.log = logger
        self.dump_dir = dump_dir
        self._current_keyword = ""
        self._pw = None
        self._ctx: BrowserContext | None = None
        self._captures: list[tuple[int, dict, float]] = []
        self._capture_since = 0.0
        self.stats = {"search_requests": 0}
        self.sort_status: dict[str, str] = {}
        self._last_sort_status = "未执行"
        # 预算计数钩子：每发出一个搜索请求回调一次，由引擎挂载。
        self.on_request: Callable[[], None] | None = None
        # 翻页失败时保留已抓数据，把原因留在这里交给引擎记账。
        self.last_search_error = ""

    # ---------------------------------------------------------------- lifecycle
    def __enter__(self) -> "Crawler":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def start(self) -> None:
        """启动浏览器。loop 控制台跨轮复用，只调用一次。"""
        if self._ctx is not None:
            return
        prepare_browser_temp(self.cfg)
        self._pw = sync_playwright().start()
        self._ctx = self._launch(self.headless)

    def stop(self) -> None:
        try:
            if self._ctx is not None:
                self._ctx.close()
        except Exception:  # noqa: BLE001 - 关闭失败不阻塞收尾
            pass
        finally:
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:  # noqa: BLE001
                    pass
            self._ctx = None
            self._pw = None

    @property
    def request_total(self) -> int:
        """本轮浏览器累计发出的页面请求数。"""
        return int(self.stats.get("search_requests", 0))

    def _launch(self, headless: bool) -> BrowserContext:
        assert self._pw is not None
        profile = self.cfg.browser.profile_dir
        profile.mkdir(parents=True, exist_ok=True)
        options: dict[str, Any] = {
            "user_data_dir": str(profile),
            "headless": headless,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "viewport": {"width": 1440, "height": 900},
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                *stealth_launch_args(),
            ],
            "ignore_default_args": stealth_ignore_default_args(),
        }
        if self.cfg.browser.channel:
            options["channel"] = self.cfg.browser.channel
        configured_ua = self.cfg.browser.user_agent
        if configured_ua:
            options["user_agent"] = configured_ua
        elif headless:
            options["user_agent"] = default_user_agent()
        context = self._pw.chromium.launch_persistent_context(**options)
        context.set_default_timeout(self.cfg.browser.timeout_seconds * 1000)
        # 预算按“实际发出的搜索接口请求”计数：响应慢/解析失败也不能漏算。
        context.on("request", self._on_request)
        # 被动监听：不接管、不重发请求，少一处可被观察的干预面（借鉴方案三）
        context.on("response", self._on_response)
        count = apply_stealth(self.cfg, context)
        if count:
            self.log(f"  · 已注入 {count} 条反检测脚本（Scrapling bypasses）")
        return context

    def _on_request(self, request: Any) -> None:
        if sel.SEARCH_API_PATH in (request.url or ""):
            self.stats["search_requests"] += 1
            if self.on_request is not None:
                try:
                    self.on_request()
                except Exception as exc:  # noqa: BLE001 - 计数失败不能打断抓取
                    self.log(f"  ! 请求预算计数失败：{exc}")

    def _on_response(self, response: Any) -> None:
        if sel.SEARCH_API_PATH not in (response.url or ""):
            return
        try:
            page_number = _page_number_from_request(response.request)
            payload = json.loads(response.body().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - 响应体可能已被浏览器丢弃，等下一份就行
            return
        self._captures.append((page_number, payload, time.monotonic()))
        self._dump_payload(page_number, payload)

    def _dump_payload(self, page_number: int, payload: dict) -> None:
        """--dump 时把接口原始返回落盘，用来生成 tests/fixtures 的真实样本。"""
        if self.dump_dir is None:
            return
        try:
            self.dump_dir.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r'[\\/:*?"<>|\s]+', "_", self._current_keyword).strip("_")
            prefix = f"search_{slug}" if slug else "search"
            name = f"{prefix}_p{page_number}_{int(time.time() * 1000)}.json"
            (self.dump_dir / name).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            self.log(f"  · 原始响应落盘失败：{exc}")

    def _wait_capture(self, page_number: int, page: Page | None = None, timeout_ms: int = CAPTURE_TIMEOUT_MS) -> dict:
        deadline = time.monotonic() + timeout_ms / 1000
        ticks = 0
        while time.monotonic() < deadline:
            for index, (number, payload, _captured_at) in enumerate(self._captures):
                if number == page_number:
                    self._captures.pop(index)
                    return payload
            ticks += 1
            if page is not None and ticks % 10 == 0:
                # 有头模式下可能停下来等主人滑滑块，等待时间不算在抓取超时里
                deadline += self._check_page_state(page)
            time.sleep(0.2)
        raise CaptureTimeout(
            f"等待第 {page_number} 页搜索结果超时：页面没有发出搜索请求"
            "（改版就改 xianyu/selectors.py，不是只能等冷却）"
        )

    def _wait_capture_settled(
        self,
        page_number: int,
        page: Page | None = None,
        timeout_ms: int = FILTER_CAPTURE_TIMEOUT_MS,
    ) -> dict:
        """等筛选请求都落地：收到结果后再等一小段静默期，取最后一次。"""
        deadline = time.monotonic() + timeout_ms / 1000
        last: dict | None = None
        settled_at: float | None = None
        ticks = 0
        while time.monotonic() < deadline:
            found = None
            for index, (number, _payload, captured_at) in enumerate(self._captures):
                if number == page_number and captured_at >= self._capture_since:
                    found = index
            if found is not None:
                _number, last, _captured_at = self._captures.pop(found)
                settled_at = time.monotonic()
            elif last is not None and settled_at is not None:
                if (time.monotonic() - settled_at) * 1000 >= FILTER_SETTLE_MS:
                    return last
            ticks += 1
            if page is not None and ticks % 10 == 0:
                deadline += self._check_page_state(page)
            time.sleep(0.2)
        if last is not None:
            return last
        raise CaptureTimeout(f"等待第 {page_number} 页搜索结果超时")

    def _dismiss_ad_popup(self, page: Page) -> None:
        close = page.locator(sel.AD_CLOSE_BUTTON)
        try:
            if close.count():
                close.first.click(timeout=1200)
                self.log("  · 已关闭广告弹窗")
        except Exception:  # noqa: BLE001 - 弹窗可有可无
            pass

    @staticmethod
    def _new_publish_state(page: Page) -> bool | None:
        """页面当前是否已选「最新」；找不到筛选控件返回 None。"""
        try:
            return page.evaluate(
                """() => {
                    const titles = [...document.querySelectorAll('span[class*="search-select-title"]')];
                    const texts = titles.map(el => el.textContent.trim());
                    if (texts.includes('最新')) return true;
                    if (texts.includes('新发布') || texts.includes('1天内')) return false;
                    return null;
                }"""
            )
        except Exception:  # noqa: BLE001 - 页面跳转/关闭时读不到
            return None

    def _assert_on_search_page(self, page: Page, before_pages: int | None) -> None:
        """排序点击后只允许留在搜索页；点进商品/卖家页或开了新标签就停本轮。"""
        if self._ctx is not None and before_pages is not None:
            opened = self._ctx.pages[before_pages:]
            if opened:
                for extra in opened:
                    try:
                        extra.close()
                    except Exception:  # noqa: BLE001 - 关不掉也要停
                        pass
                self._last_sort_status = "点击后打开了新页面，本轮停止"
                raise CrawlError("点排序时打开了新页面（可能点到商品），本轮停止")
        url = page.url or ""
        if not url:
            return
        parts = urlsplit(url)
        if parts.netloc != sel.SEARCH_HOST or not parts.path.startswith(sel.SEARCH_PATH):
            self._last_sort_status = "点击后离开搜索页，本轮停止"
            raise CrawlError(f"点排序后离开了搜索页（{url}），本轮停止")

    @staticmethod
    def _click_text_in_panel(panel, text: str) -> bool:
        """在筛选面板内用 DOM click 点文本。

        搜索页有一层全屏图片遮罩，Playwright 的 actionability 检查会被它挡下；
        DOM click 只命中面板内这个文本节点，不依赖坐标，也不会点到商品卡片。
        """
        target = panel.get_by_text(text, exact=True).first
        if target.count() == 0:
            return False
        target.evaluate("el => el.click()")
        return True

    def _apply_newest_filter(self, page: Page, baseline: dict) -> dict:
        """点「新发布 → 最新」，用排序后的第 1 页覆盖初始结果。

        只允许在筛选面板内点，点不到就沿用初始结果；返回顺序仍由 publishTime 反查。
        「最新」与「1天内」是同组互斥子项：点「最新」拿真时间序，
        24 小时时间窗由客户端 scan.max_age_hours 自己筛。
        """
        self._last_sort_status = "未执行"
        if not self.cfg.scan.newest_first:
            self._last_sort_status = "已关闭"
            return baseline
        try:
            page.wait_for_selector(sel.SEARCH_FILTER_PANEL, timeout=5000, state="attached")
        except PWTimeout:
            pass
        state = self._new_publish_state(page)
        if state is True:
            self._last_sort_status = "已是「最新」（未重复点）"
            self.log("  · 已是「最新」")
            return baseline
        if state is None:
            self._last_sort_status = "未找到排序控件，按默认排序"
            self.log("  ! 页面上没找到「新发布 / 最新」，本轮按默认排序")
            return baseline

        panel = page.locator(sel.SEARCH_FILTER_PANEL).first
        if panel.count() == 0:
            self._last_sort_status = "未找到筛选面板，按默认排序"
            self.log("  ! 没找到筛选面板，未点筛选，本轮按默认排序")
            return baseline

        self._captures.clear()
        # 只认点击之后到达的响应，避免首屏的迟到响应抢在筛选结果后面被当成最终结果。
        self._capture_since = time.monotonic()
        before_pages = len(self._ctx.pages) if self._ctx is not None else None
        try:
            if not self._click_text_in_panel(panel, sel.NEW_PUBLISH_FILTER_TEXT):
                raise LookupError("未找到「新发布」")
        except Exception:  # noqa: BLE001 - 点不到就降级，不影响本轮
            self._check_page_state(page)
            self._assert_on_search_page(page, before_pages)
            self._last_sort_status = "点筛选失败，按默认排序"
            self.log("  ! 点「新发布 → 最新」失败，沿用当前排序结果")
            return baseline
        self._assert_on_search_page(page, before_pages)

        time.sleep(random.uniform(0.6, 1.2))
        try:
            if not self._click_text_in_panel(panel, sel.NEW_PUBLISH_OPTION_TEXT):
                raise LookupError("未找到「最新」")
        except Exception:  # noqa: BLE001
            self._check_page_state(page)
            self._assert_on_search_page(page, before_pages)
            self._last_sort_status = "点筛选失败，按默认排序"
            self.log("  ! 点「新发布 → 最新」失败，沿用当前排序结果")
            return baseline
        self._assert_on_search_page(page, before_pages)
        self._last_sort_status = "已点「新发布 → 最新」"
        self.log("  → 已选「新发布 / 最新」")
        try:
            payload = self._wait_capture_settled(1, page, timeout_ms=FILTER_CAPTURE_TIMEOUT_MS)
        except CaptureTimeout:
            self._check_page_state(page)
            self._assert_on_search_page(page, before_pages)
            self._last_sort_status = "点筛选后没等到新结果，沿用原结果"
            self.log("  ! 选「最新」后没等到新结果，沿用当前结果")
            return baseline
        self._assert_on_search_page(page, before_pages)
        return payload

    def _handle_baxia(self, page: Page) -> float:
        """Baxia 弹窗：有头时停下来等主人手动滑，返回等待秒数；过不了才判风控。

        借鉴待分析项目2：弹窗元素比正文提示更早出现，先查它。
        """
        try:
            popup = page.locator(sel.BAXIA_SELECTORS).first
            if popup.count() == 0:
                return 0.0
        except Exception:  # noqa: BLE001 - 查不到就当没有
            return 0.0
        if self.headless:
            raise RiskControl("出现 Baxia 人机验证弹窗；无头模式过不了，请用有头模式重试")
        wait = max(10, int(self.cfg.browser.baxia_wait_seconds))
        self.log(f"  ⚠ 出现人机验证弹窗：请在浏览器里手动滑一下（最多等 {wait} 秒）…")
        began = time.monotonic()
        deadline = began + wait
        while time.monotonic() < deadline:
            time.sleep(2)
            try:
                if popup.count() == 0:
                    waited = time.monotonic() - began
                    self.log(f"  ✓ 验证已通过（等了 {waited:.0f} 秒），本轮继续")
                    return waited
            except Exception:  # noqa: BLE001 - 页面跳转会让 locator 失效
                return time.monotonic() - began
        raise RiskControl(f"人机验证 {wait} 秒内没有通过，本轮停止；不要连续重试")

    def _check_page_state(self, page: Page) -> float:
        """检查登录墙/限流/人机验证；返回等主人处理验证花掉的秒数。"""
        waited = self._handle_baxia(page)
        url = page.url or ""
        if sel.LOGIN_HOST in url or "/login" in url:
            raise LoginRequired("页面跳转到登录页，登录态可能已失效：请重新运行 login.bat")
        try:
            text = page.inner_text(sel.BODY, timeout=3000)
        except PWTimeout:
            return waited
        except Exception:  # noqa: BLE001
            return waited
        # 先判频率再判人机验证：限流页常顺带写着一句「请完成验证」，
        # 按人机验证处理会被升级成整天熔断，而它其实等十几分钟就能恢复。
        rate = sel.first_match(text, sel.RATE_LIMIT_TEXT_MARKERS)
        if rate:
            raise RateLimited(f"闲鱼提示「{rate}」，本轮停止，稍后再试")
        captcha = sel.first_match(text, sel.RISK_TEXT_MARKERS)
        if captcha:
            raise RiskControl(f"页面出现「{captcha}」，请在有头窗口里手动过一下，不要连续重试")
        illegal = sel.first_match(text, sel.ILLEGAL_ACCESS_MARKERS)
        if illegal:
            raise CrawlError(
                f"闲鱼把本次访问判为“{illegal}”。请用有头模式重试，"
                "或在 config.yaml 里填一个真实浏览器的 browser.user_agent"
            )
        return waited

    def _sleep(self) -> None:
        low = max(0.0, self.cfg.browser.min_delay)
        high = max(low, self.cfg.browser.max_delay)
        if high > 0:
            time.sleep(random.uniform(low, high))

    # ------------------------------------------------------------------- search
    def search(
        self,
        watch: Watch,
        pages: int | None = None,
        *,
        stop_event: threading.Event | None = None,
        known_ids: Set[str] | None = None,
    ) -> list[Listing]:
        # 先清掉上一轮同关键词的状态，避免本轮在搜索前失败时读到旧值。
        self._last_sort_status = "未执行"
        self.last_search_error = ""
        self.sort_status[watch.keyword] = "未执行"
        self._current_keyword = watch.keyword
        if self._ctx is None:
            raise CrawlError("Crawler 未启动")
        if self.cfg.browser.require_login:
            names = login_cookie_names(self._ctx)
            if sel.LOGIN_COOKIE_REQUIRED not in names:
                raise LoginRequired("未登录或登录态已失效：请先运行 login.bat 扫码登录")
            missing = [name for name in sel.LOGIN_COOKIES if name not in names]
            if missing:
                self.log(f"  · 登录 cookie 不完整（缺 {'/'.join(missing)}），本次继续但请留意")
        pages = max(1, int(pages or watch.pages or self.cfg.scan.pages))
        stop_on_known = bool(self.cfg.scan.stop_on_known) and bool(known_ids)
        page = self._ctx.new_page()
        results: list[Listing] = []
        seen: set[str] = set()
        warned_order = False
        try:
            self._captures.clear()
            url = build_search_url(watch.keyword)
            self.log(f"  → 搜索「{watch.keyword}」第 1/{pages} 页")
            try:
                page.goto(url, wait_until="domcontentloaded")
            except PWTimeout as exc:
                raise CrawlError("打开闲鱼搜索页超时，检查网络或代理") from exc
            self._check_page_state(page)
            self._dismiss_ad_popup(page)

            payload = self._wait_capture(1, page)
            payload = self._apply_newest_filter(page, payload)
            for index in range(1, pages + 1):
                if stop_event is not None and stop_event.is_set():
                    self.log("  · 收到 stop，停止继续翻页")
                    break
                listings = self._consume(payload, watch)
                if index == 1 and not listings:
                    self.log("  ! 第 1 页返回 0 条：可能是风控或关键词没有结果")
                if index == 1 and listings and self.cfg.scan.newest_first:
                    # 「点过按钮」不等于排序生效：顺序只认 publishTime 反查。
                    self._last_sort_status = (
                        f"{self._last_sort_status}，{order_note(listings)}"
                    )
                for listing in listings:
                    if listing.item_id not in seen:
                        seen.add(listing.item_id)
                        results.append(listing)
                if index >= pages:
                    break
                if stop_on_known:
                    if (
                        self.cfg.scan.newest_first
                        and listings
                        and not warned_order
                        and not looks_newest_first(listings)
                    ):
                        warned_order = True
                        self.log("  · 这一页不是新→旧：点「最新」后顺序没生效，本轮不按「老货提前停」收尾")
                    if should_stop_early(listings, known_ids or set()):
                        self.log(
                            f"  · 第 {index} 页 {len(listings)} 条全是见过的老货，"
                            "且顺序确认新→旧，提前结束翻页"
                        )
                        break
                self._sleep()
                next_payload = self._advance(page, index + 1)
                if next_payload is None:
                    # 翻页断了也要交出已抓到的页，不能把第 1 页白扔掉。
                    self.last_search_error = (
                        f"第 {index + 1} 页翻页失败或超时，"
                        f"已保留前 {len(results)} 条"
                    )
                    self.log(f"  ! {self.last_search_error}，停止继续翻页")
                    break
                payload = next_payload
            return results
        finally:
            self.sort_status[watch.keyword] = self._last_sort_status
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
            self._captures.clear()

    def _consume(self, payload: dict, watch: Watch) -> list[Listing]:
        try:
            listings, _ = parse_payload(payload)
        except ApiRejected as exc:
            text = str(exc)
            # 限流优先：它只需要等，不该被当成要人工处理的风控。
            if sel.first_match(text, sel.RATE_LIMIT_ANY_MARKERS):
                raise RateLimited(text) from exc
            if "风控" in text or sel.first_match(text, sel.API_RISK_MARKERS):
                raise RiskControl(text) from exc
            if sel.first_match(text, sel.API_LOGIN_MARKERS):
                raise LoginRequired(f"{text}（请重新运行 login.bat）") from exc
            raise CrawlError(text) from exc
        except ParseError as exc:
            raise CrawlError(f"接口结构异常，可能改版了：{exc}") from exc
        for listing in listings:
            listing.watch = watch.name
            listing.keyword = watch.keyword
        return listings

    def _advance(self, page: Page, page_number: int) -> dict | None:
        """点一次下一页；点完没响应就结束本轮，绝不换策略补点。"""
        strategies = (
            ("JS", self._click_arrow_js),
            ("页码", self._click_page_number),
            ("箭头", self._click_arrow),
        )
        for label, action in strategies:
            try:
                clicked = action(page, page_number)
            except Exception:  # noqa: BLE001 - 异常时无法确定点击是否已发出
                self.log(
                    f"  · {label}点击异常：本轮停止，不再换方式补点"
                    "（异常可能发生在点击之后）"
                )
                return None
            if not clicked:
                continue
            self.log(f"  → 翻到第 {page_number} 页（{label}）")
            try:
                return self._wait_capture(page_number, page, timeout_ms=9000)
            except CaptureTimeout:
                self.log(
                    f"  · {label}已点过但没有新请求：本轮停止，不再换方式补点"
                )
                return None
        return None

    def _click_page_number(self, page: Page, page_number: int) -> bool:
        container = page.locator(sel.PAGINATION_CONTAINER).first
        try:
            container.scroll_into_view_if_needed(timeout=3000)
        except Exception:  # noqa: BLE001
            pass
        box = (
            page.locator(sel.PAGINATION_PAGE_BOX)
            .filter(has_text=re.compile(rf"^\s*{page_number}\s*$"))
            .first
        )
        if box.count() == 0:
            return False
        box.click(timeout=4000)
        return True

    def _click_arrow(self, page: Page, _page_number: int) -> bool:
        button = page.locator(sel.NEXT_PAGE).first
        button.wait_for(state="visible", timeout=4000)
        button.scroll_into_view_if_needed(timeout=3000)
        button.click(timeout=4000, force=True)
        return True

    def _click_arrow_js(self, page: Page, _page_number: int) -> bool:
        # 选择器当参数传进去：JS 源码里的 {} 和 Python 的 f-string 打架。
        return bool(
            page.evaluate(
                """(selector) => {
                    const nodes = [...document.querySelectorAll(selector)];
                    const next = nodes[nodes.length - 1];
                    if (!next) return false;
                    next.scrollIntoView({block: 'center'});
                    next.click();
                    return true;
                }""",
                sel.NEXT_PAGE_JS,
            )
        )
