"""闲鱼页面上所有「会被改版影响」的匹配串，集中一处。

页面改版时只改这个文件；其余模块一律不写死 class 名、接口路径或提示语。
标记串按用途分组：人机验证（RiskControl）、频率限制（短退避）、Baxia 弹窗（等人工）、
登录墙、改版（软失败）。

方案二特有：登录态判断用 second-eye 的四个 cookie（unb/sn/uc1/lgc），
Baxia 选择器来自待分析项目2 反编译出的在跑代码。
"""
from __future__ import annotations

from typing import Iterable

# --------------------------------------------------------------------------- 地址
BASE_URL = "https://www.goofish.com"
SEARCH_HOST = "www.goofish.com"
SEARCH_PATH = "/search"
SEARCH_URL = BASE_URL + SEARCH_PATH + "?q={keyword}"
LOGIN_URL = BASE_URL + "/login"
SELLER_URL = BASE_URL + "/personal?userId={seller_id}"
LOGIN_HOST = "passport.goofish.com"

# 搜索页新发布筛选只能点 UI 上的「新发布 → 最新」。
# 2026-10-22 登录态真实抓包 + 真实库 A/B 对照证明：
# - 点「1天内」只加时间窗（服务端 hasFilterWhenFirstPage=true，
#   hasSortWhenFirstPage / enableNewPubRank 仍是 false），返回不是新→旧（#4 轮实测）；
# - 点「最新」给出真时间序（#3 轮 sort_status = 顺序已确认新→旧）。
# 搜索地址里的 sortType=time 则会被服务端忽略，返回还是综合排序。
# 「最新」与「1天内」是同组互斥子项，只能二选一：点「最新」拿排序，
# 24 小时时间窗交给客户端（scan.max_age_hours）自己筛。
SEARCH_FILTER_PANEL = "div[class*='search-filter']"
NEW_PUBLISH_FILTER_TEXT = "新发布"
NEW_PUBLISH_OPTION_TEXT = "最新"
NEW_PUBLISH_TITLE = "span[class*='search-select-title']"

# 搜索接口：抓取层靠被动监听这个路径的响应拿数据，不自己签名。
SEARCH_API_PATH = "/h5/mtop.taobao.idlemtopsearch.pc.search/1.0"
SEARCH_API = "mtop.taobao.idlemtopsearch.pc.search"

# ----------------------------------------------------------------------- 登录 cookie
# 借鉴 second-eye：登录态不只看 unb，四个 cookie 齐全才算真的登录。
# 方案二的账号就是主人的闲鱼账号，登录态失效要立刻停，绝不带着半登录态继续抓。
LOGIN_COOKIES = ("unb", "sn", "uc1", "lgc")
LOGIN_COOKIE_REQUIRED = "unb"

# ----------------------------------------------------------------------- DOM 选择器
# 闲鱼前端是 CSS Modules 哈希类名（search-pagination-arrow-container-1a2b3c），
# 只能用 [class*=...] 前缀模糊匹配；哈希后缀变了不影响，组件名变了才算改版。
PAGINATION_CONTAINER = "[class*='search-pagination-container']"
PAGINATION_PAGE_BOX = "[class*='search-pagination-page-box']"
PAGINATION_ARROW_CONTAINER = "search-pagination-arrow-container"
PAGINATION_ARROW_RIGHT = "search-pagination-arrow-right"
NEXT_PAGE = (
    f"button[class*='{PAGINATION_ARROW_CONTAINER}']"
    f":has([class*='{PAGINATION_ARROW_RIGHT}'])"
    ":not([disabled])"
)
# JS 兜底点击用不到 :has()，单独给一条 querySelectorAll 能吃的选择器。
NEXT_PAGE_JS = f"button[class*='{PAGINATION_ARROW_CONTAINER}']"
AD_CLOSE_BUTTON = "div[class*='closeIconBg']"
BODY = "body"

# ------------------------------------------------------------------- 页面提示语标记
# 人机验证：需要人工过，属于真正的风控，走按天起跳的熔断梯子。
RISK_TEXT_MARKERS = ("滑动验证", "安全验证", "请完成验证", "验证码", "人机验证")
# 频率/额度：多为「刷太快」的软限流，短退避即可解除，不该升级成整天冷却。
RATE_LIMIT_TEXT_MARKERS = (
    "请求频率或额度已超限",
    "频率或额度已超限",
    "访问过于频繁",
    "操作过于频繁",
)
# 登录墙。
LOGIN_WALL_MARKERS = ("登录后", "扫码")
ILLEGAL_ACCESS_MARKERS = ("非法访问",)

# ------------------------------------------------------------------- 接口 ret 标记
# 判定顺序：先频率、再验证，因为 USER_VALIDATE 类消息里常同时带「频率」字样。
API_RATE_LIMIT_MARKERS = ("TRAFFIC", "RATE_LIMIT", "TOO_MANY", "RETRY_LATER")
API_RISK_MARKERS = ("RGV587", "PUNISH", "USER_VALIDATE")
API_LOGIN_MARKERS = ("SESSION", "LOGIN", "TOKEN")
RATE_LIMIT_ANY_MARKERS = RATE_LIMIT_TEXT_MARKERS + API_RATE_LIMIT_MARKERS

# 改版迹象关键词：冷却走软失败档，并提示去改本文件，而不是等 7 天。
REDESIGN_MARKERS = ("可能改版", "结构异常", "缺少 data", "resultList 不是数组", "不是 JSON 对象")

# --------------------------------------------------------------------- Baxia 风控弹窗
# 借鉴待分析项目2：先查弹窗再读正文，有头模式下停下来等主人手动滑，而不是直接判失败。
# 故意不收 div[class*='risk']——太宽，正常页面也可能命中。
BAXIA_SELECTORS = (
    "div.baxia-dialog, div[class*='baxia'], #nc_mask, .nc-container, .nc_iconfont"
)
BAXIA_TEXT_MARKERS = ("滑块", "拖动", "请完成验证", "安全验证")


def first_match(text: str, markers: Iterable[str]) -> str:
    """返回文本里第一个命中的标记词，没命中返回空串。

    命中词会写进错误消息，熔断时能看清是哪条规则触发的。
    英文标记按大写比对：mtop 的 ret 全大写，中文标记不受影响。
    """
    haystack = str(text or "").upper()
    for marker in markers:
        if str(marker).upper() in haystack:
            return marker
    return ""
