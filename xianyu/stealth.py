"""反检测加固：向浏览器注入 bypass 脚本 + 统一启动参数。

借鉴自 Scrapling v0.3.10（BSD-3-Clause，https://github.com/jasonking0112/Scrapling），
取其 `engines/toolbelt/bypasses` 脚本与启动参数思路。

## 保留的 5 个脚本（按序注入，顺序有意义）

- webdriver_fully.js            navigator.webdriver 伪装成原生 getter
- window_chrome.js              补全 window.chrome（app/csi/loadTimes）
- navigator_plugins.js          补 5 个 PDF 插件
- notification_permission.js    Notification.permission 固定 default
- playwright_fingerprint.js     删除 __pwInitScripts / __playwright__binding__（Playwright 运行时痕迹）

## 实测结论（2026-10-22，Chrome 151 + Playwright 当前版，headless 对照实验）

1. navigator.webdriver 在新版 Chrome 下由 Blink 内部控制，JS defineProperty 盖不住；
   真正让它变 false 的是启动参数 `--disable-blink-features=AutomationControlled`
   （两个方案的 crawler 一直在传）。webdriver_fully.js 在当前环境下空转，保留只为兜底。
2. navigator.plugins 在新版 headless Chrome 自带 5 个 PDF 插件，plugins.js 的
   `length == 0` 条件不触发；有头真实 Chrome 更不缺。空转，无害。
3. window.chrome 在真实 Chrome 里本就存在，window_chrome.js 不触发。空转，无害。
4. Notification.permission 在 https 下默认就是 default，脚本无增量。空转，无害。
5. __pwInitScripts / __playwright__binding__ 在当前 Playwright 版本已不存在，
   delete 不存在的属性是空操作。保留作为版本兜底：一旦 Playwright 换回旧机制即生效。
6. **未搬 screen_props.js**：上游把采集机的 outerWidth=1313 等写死进页面。实测脚本在
   文档早期执行（此时 outerWidth 还是 0），会把新版 headless 的真实窗口值 1280 覆盖成
   1313——不是修缺陷，是制造不一致。现代 Chrome 的 outerWidth 已非 0，此脚本无存在理由。

## 结论

这套脚本在当前环境（新版 Chrome + 当前 Playwright）下基本空转，核心防护来自启动参数
（AC + ignore_default_args）。保留注入的成本是 5 个小文件，价值是版本兜底；
若将来 Playwright 降级或检测面变化，它们会重新发挥作用。不要因为它们的存在
而低估「低频 + 真实有头 Chrome + 长期 profile」这套主药的作用。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_BYPASS_DIR = Path(__file__).parent / "stealth_bypasses" / "bypasses"

# 顺序与上游一致，不要调整（上游注释明确说顺序有意义）
_BYPASS_FILES = (
    "webdriver_fully.js",
    "window_chrome.js",
    "navigator_plugins.js",
    "notification_permission.js",
    "playwright_fingerprint.js",
)


@lru_cache(maxsize=1)
def load_bypasses() -> tuple[str, ...]:
    """读取并按序返回全部 bypass 脚本内容。"""
    scripts: list[str] = []
    for name in _BYPASS_FILES:
        path = _BYPASS_DIR / name
        if not path.is_file():
            raise FileNotFoundError(f"缺少反检测脚本：{path}")
        scripts.append(path.read_text(encoding="utf-8"))
    return tuple(scripts)


def bypass_count() -> int:
    return len(_BYPASS_FILES)


def apply_stealth(cfg, context) -> int:
    """向 context 注入 bypass 脚本；返回注入条数。

    自启动和 CDP 接管两条路径都可调用。CDP 复用已有 context 时同样有效
    （对之后新建的页面生效；用户已打开的标签页不受影响，也不该受影响）。
    配置 browser.stealth=false 时跳过。
    """
    browser = getattr(cfg, "browser", None)
    if browser is not None and not getattr(browser, "stealth", True):
        return 0
    injected = 0
    for script in load_bypasses():
        context.add_init_script(script=script)
        injected += 1
    return injected


def stealth_ignore_default_args() -> list[str]:
    """启动时要去掉的 Playwright 默认参数（上游 HARMFUL_DEFAULT_ARGS）。

    --enable-automation 会暴露自动化开关；其余几个是无用默认项，
    去掉后启动参数更接近手动打开的 Chrome。
    """
    return [
        "--enable-automation",
        "--disable-popup-blocking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
    ]


def stealth_launch_args() -> list[str]:
    """追加的启动参数（上游 DEFAULT_STEALTH_FLAGS 的无害子集）。

    刻意不搬：--incognito（与长期复用 profile 冲突）、--lang/--accept-lang（中文场景）、
    --test-type（无收益）。

    注意：最关键的 `--disable-blink-features=AutomationControlled` 由 crawler 的
    启动参数直接传入（实测这是新版 Chrome 下 navigator.webdriver 唯一有效的开关），
    不在这里重复提供，避免两处维护同一件事。
    """
    return [
        "--disable-sync",
        "--mute-audio",
        "--hide-scrollbars",
        "--disable-translate",
        "--disable-breakpad",
        "--disable-session-crashed-bubble",
    ]
