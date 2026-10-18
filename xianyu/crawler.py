"""Playwright 抓取：复用本机 Chrome 的登录态，拦截闲鱼 PC 搜索接口。

为什么不直接请求接口：闲鱼接口有签名 + 风控（RGV587），裸请求必被拦。
这里让真实浏览器自己发请求，我们只旁路读取返回的 JSON，签名/风控都由页面完成。
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from playwright.sync_api import BrowserContext, Page, Route, TimeoutError as PWTimeout, sync_playwright

from .config import Config, Watch
from .models import Listing
from .parse import ApiRejected, ParseError, parse_payload

BASE_URL = "https://www.goofish.com"
SEARCH_URL = BASE_URL + "/search?q={keyword}"
NEXT_PAGE = (
    "button[class*='search-pagination-arrow-container']"
    ":has([class*='search-pagination-arrow-right'])"
    ":not([disabled])"
)
CAPTURE_TIMEOUT_MS = 25_000
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
    """把 Playwright/Chrome 的临时目录放到项目内。

    有些 Windows 机器的系统临时目录权限异常（mkdtemp 报 EPERM），
    用项目自己的 data/browser_tmp 最稳，也方便一起清理。
    """
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


class CaptureTimeout(CrawlError):
    """页面没发出搜索请求（可能被风控或页面改版）。"""


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


class Crawler:
    """一个浏览器上下文里依次跑完所有关键词。"""

    def __init__(self, cfg: Config, *, headless: bool | None = None, logger: Callable[[str], None] = print):
        self.cfg = cfg
        self.headless = cfg.browser.headless if headless is None else headless
        self.log = logger
        self._pw = None
        self._ctx: BrowserContext | None = None
        self._captures: list[tuple[int, dict]] = []

    # ---------------------------------------------------------------- lifecycle
    def __enter__(self) -> "Crawler":
        prepare_browser_temp(self.cfg)
        self._pw = sync_playwright().start()
        self._ctx = self._launch(self.headless)
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self._ctx is not None:
                self._ctx.close()
        finally:
            if self._pw is not None:
                self._pw.stop()
            self._ctx = None
            self._pw = None

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
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        if self.cfg.browser.channel:
            options["channel"] = self.cfg.browser.channel
        configured_ua = self.cfg.browser.user_agent
        if configured_ua:
            options["user_agent"] = configured_ua
        elif headless:
            # 无头 Chrome 默认 UA 带 "HeadlessChrome"，闲鱼会直接返回“非法访问”。
            options["user_agent"] = default_user_agent()
        context = self._pw.chromium.launch_persistent_context(**options)
        context.set_default_timeout(self.cfg.browser.timeout_seconds * 1000)
        # 只拦截真正的搜索接口：...pc.search/1.0/。
        # 注意 "idlemtopsearch.pc.search.shade" 也包含这个前缀，必须带结尾的 /1.0 才不会误抓。
        context.route(
            lambda url: "/h5/mtop.taobao.idlemtopsearch.pc.search/1.0" in url,
            self._on_route,
        )
        return context

    # ---------------------------------------------------------------- capturing
    def _on_route(self, route: Route) -> None:
        try:
            response = route.fetch(max_redirects=0)
            body = response.body()
            route.fulfill(response=response, body=body)
        except Exception:  # noqa: BLE001 - 抓不到就放行，不影响页面
            try:
                route.continue_()
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return
        page_number = _page_number_from_request(route.request)
        self._captures.append((page_number, payload))

    def _wait_capture(self, page_number: int, page: Page | None = None, timeout_ms: int = CAPTURE_TIMEOUT_MS) -> dict:
        deadline = time.monotonic() + timeout_ms / 1000
        ticks = 0
        while time.monotonic() < deadline:
            for index, (number, payload) in enumerate(self._captures):
                if number == page_number:
                    self._captures.pop(index)
                    return payload
            ticks += 1
            if page is not None and ticks % 10 == 0:
                self._check_page_state(page)
            time.sleep(0.2)
        raise CaptureTimeout(f"等待第 {page_number} 页搜索结果超时")

    # ------------------------------------------------------------------ helpers
    def _check_page_state(self, page: Page) -> None:
        url = page.url or ""
        if "passport.goofish.com" in url or "/login" in url:
            raise LoginRequired("登录态失效，请重新运行：python -m xianyu login")
        try:
            text = page.inner_text("body", timeout=3000)
        except PWTimeout:
            return
        except Exception:  # noqa: BLE001
            return
        if any(word in text for word in ("滑动验证", "安全验证", "请完成验证", "验证码")):
            raise RiskControl("触发人机验证，请用有头模式手动过一下，或稍后再试")
        if "非法访问" in text:
            raise CrawlError(
                "闲鱼把本次访问判为“非法访问”。请用 run_scan.bat --headed 重试；"
                "或在 config.yaml 里填一个真实浏览器的 browser.user_agent"
            )
        if "登录后" in text and "扫码" in text and len(text) < 3000:
            raise LoginRequired("页面要求登录，请重新运行：python -m xianyu login")

    def _sleep(self) -> None:
        low = max(0.0, self.cfg.browser.min_delay)
        high = max(low, self.cfg.browser.max_delay)
        if high > 0:
            time.sleep(random.uniform(low, high))

    # ------------------------------------------------------------------- search
    def search(self, watch: Watch, pages: int | None = None) -> list[Listing]:
        if self._ctx is None:
            raise CrawlError("Crawler 未启动")
        if self.cfg.browser.require_login:
            names = {cookie["name"] for cookie in self._ctx.cookies()}
            if "unb" not in names:
                raise LoginRequired("未登录或登录态已失效：请先运行 run_login.bat 扫码登录")
        pages = max(1, int(pages or watch.pages or self.cfg.scan.pages))
        page = self._ctx.new_page()
        results: list[Listing] = []
        seen: set[str] = set()
        try:
            self._captures.clear()
            url = SEARCH_URL.format(keyword=quote(watch.keyword))
            self.log(f"  → 搜索「{watch.keyword}」第 1/{pages} 页")
            try:
                page.goto(url, wait_until="domcontentloaded")
            except PWTimeout as exc:
                raise CrawlError("打开闲鱼搜索页超时，检查网络或代理") from exc
            self._check_page_state(page)

            payload = self._wait_capture(1, page)
            for index in range(1, pages + 1):
                listings = self._consume(payload, watch)
                if index == 1 and not listings:
                    self.log("  ! 第 1 页返回 0 条：可能是未登录、风控或关键词没有结果")
                for listing in listings:
                    if listing.item_id not in seen:
                        seen.add(listing.item_id)
                        results.append(listing)
                if index >= pages:
                    break
                self._sleep()
                next_payload = self._advance(page, index + 1)
                if next_payload is None:
                    self.log("  · 已到最后一页")
                    break
                payload = next_payload
            return results
        finally:
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
            if "风控" in text:
                raise RiskControl(text) from exc
            if "登录" in text:
                raise LoginRequired(text) from exc
            raise CrawlError(text) from exc
        except ParseError as exc:
            raise CrawlError(f"接口结构异常，可能改版了：{exc}") from exc
        for listing in listings:
            listing.watch = watch.name
            listing.keyword = watch.keyword
        return listings

    def _advance(self, page: Page, page_number: int) -> dict | None:
        strategies = (
            ("JS", self._click_arrow_js),
            ("页码", self._click_page_number),
            ("箭头", self._click_arrow),
        )
        for label, action in strategies:
            try:
                if not action(page, page_number):
                    continue
            except Exception:  # noqa: BLE001
                continue
            self.log(f"  → 翻到第 {page_number} 页（{label}）")
            try:
                return self._wait_capture(page_number, page, timeout_ms=9000)
            except CaptureTimeout:
                self.log(f"  · {label}点击后没有新请求，换一种方式")
        return None

    def _click_page_number(self, page: Page, page_number: int) -> bool:
        container = page.locator("[class*='search-pagination-container']").first
        try:
            container.scroll_into_view_if_needed(timeout=3000)
        except Exception:  # noqa: BLE001
            pass
        box = (
            page.locator("[class*='search-pagination-page-box']")
            .filter(has_text=re.compile(rf"^\s*{page_number}\s*$"))
            .first
        )
        if box.count() == 0:
            return False
        box.click(timeout=4000)
        return True

    def _click_arrow(self, page: Page, _page_number: int) -> bool:
        button = page.locator(NEXT_PAGE).first
        button.wait_for(state="visible", timeout=4000)
        button.scroll_into_view_if_needed(timeout=3000)
        button.click(timeout=4000, force=True)
        return True

    def _click_arrow_js(self, page: Page, _page_number: int) -> bool:
        return bool(
            page.evaluate(
                """() => {
                    const nodes = [...document.querySelectorAll("button[class*='search-pagination-arrow-container']")];
                    const next = nodes[nodes.length - 1];
                    if (!next) return false;
                    next.scrollIntoView({block: 'center'});
                    next.click();
                    return true;
                }"""
            )
        )


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
    return bool(data.get("unb"))


def do_login(cfg: Config, timeout_seconds: int = 300, logger: Callable[[str], None] = print) -> dict:
    """打开有头 Chrome，等用户扫码，检测到登录后写标记文件。"""
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
        page.goto(BASE_URL + "/login", wait_until="domcontentloaded")
        logger("")
        logger("  已打开浏览器，请用闲鱼 App 扫码登录。")
        logger(f"  登录成功后脚本会自动继续（最多等 {timeout_seconds} 秒）…")
        deadline = time.monotonic() + timeout_seconds
        unb = ""
        while time.monotonic() < deadline:
            cookies = {cookie["name"]: cookie["value"] for cookie in context.cookies()}
            unb = cookies.get("unb", "")
            if unb and unb not in {"0", "null"}:
                break
            time.sleep(2)
        if not unb or unb in {"0", "null"}:
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
        login_state_path(cfg).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger(f"  ✓ 登录成功（用户 ID {unb}），登录态已保存")
        time.sleep(1)
        context.close()
        return payload
