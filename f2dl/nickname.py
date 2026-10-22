# -*- coding: utf-8 -*-
"""作者昵称获取与清洗（Q-NICKNAME，2026-10-13 修订）：链接 → 作者昵称 → 默认任务别名。

数据通道（2026-10-13 用户决策：不查本地缓存，直接从用户页面在线爬取）：
1. 主页链接（sec_user_id）→ Playwright 驱动 Firefox（无头 + 项目内置
   便携内核）打开 https://www.douyin.com/user/{sec_uid}，轮询 DOM 提取
   昵称。2026-10-13 探针实测：未登录无 cookie 可看主页（无登录墙），
   CSR 渲染约 6~7 秒出 h1——实测 h1 即纯昵称文本（如"小水一号"），
   [data-e2e="user-info"] 首行同为昵称（作回退）。
2. 单作品链接（one 模式，aweme_id）→ 移动 Feed 免 cookie 通道在线探测
   （f2/apps/douyin/api.py 的 MOBILE_FEED 主备节点，~200ms，免 Argus）。

历史注记：PC 主页对 httpx 直拿是"无数据 CSR 空壳"（crawler.py P0 探针
结论，勿改用 httpx）；share/user 同为空壳。真实浏览器渲染是唯一可靠
通道，与作品列表浏览器通道（browser_fetch.py）同思路。

清洗（Q-NICKNAME 用户要求）：emoji/变体选择符/控制字符等特殊字符移除
+ sanitize_name（Windows 非法字符/保留名/空白折叠）；清洗后为空（纯
emoji 昵称）返回空串由调用方回退。
"""

import os
import re
import time
from typing import Optional, Tuple

from .apps import TOOL_ROOT, extract_identifier, sanitize_name

#: 项目内置 Playwright 内核目录（便携化，与 browser_fetch.py 同思路：
#: 存在则在驱动进程启动前指过去，整个项目目录拷到任何电脑都能用；
#: 缺失回退系统默认位置，零破坏）
_PROJECT_BROWSERS_DIR = TOOL_ROOT / "ms-playwright"

#: 用户主页 URL 模板
_PROFILE_URL = "https://www.douyin.com/user/{sec_uid}"

#: 页面加载超时（毫秒）——domcontentloaded 后 CSR 渲染还需数秒
_GOTO_TIMEOUT_MS = 45_000

#: DOM 轮询预算（秒）——实测未登录渲染约 6~7 秒出 h1，15 秒宽裕；
#: 轮询间隔沿用浏览器通道节奏（0.5s）
_DOM_POLL_SEC = 15
_DOM_POLL_INTERVAL_MS = 500

#: 在线探测超时（秒）——移动 Feed 实测 ~200ms，3s 已是宽裕上限；
#: 主备节点最坏 6s，fail-fast 不拖慢向导/CLI 交互。
_ONLINE_TIMEOUT_SEC = 3.0

#: emoji/装饰符号 Unicode 区段（移除后不影响中文/英文/数字/常规标点）。
#: 覆盖主流 emoji 面（含扩展）、杂项符号、装饰符号、键帽/变体选择符/
#: 零宽连接符等组合修饰符。
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # 各类 emoji 符号（含扩展面）
    "\U00002600-\U000027BF"  # 杂项符号与装饰符号（☀-➿）
    "\U00002B00-\U00002BFF"  # 杂项符号及箭头（⬀-⯿，含 ⭐）
    "\U00002300-\U000023FF"  # 杂项技术（⌚⏰⏳ 等）
    "\U0000FE00-\U0000FE0F"  # 变体选择符（emoji 修饰）
    "\U0000200D"             # 零宽连接符（emoji 序列拼接）
    "\U000020E3"             # 组合包围键帽
    "\U00002194-\U000021AA"  # 常用箭头装饰（↔-↪）
    "]+"
)

#: 控制字符（\t\n\r 留给 sanitize_name 统一折叠）
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_nickname(raw: Optional[str]) -> str:
    """昵称 → 安全别名：移除 emoji/控制字符，再走 sanitize_name。

    清洗后为空（纯 emoji / 纯特殊字符 / 空输入）返回 ""，由调用方
    回退既有默认（sec_uid 标识或手动输入）。
    """
    text = (raw or "").strip()
    text = _EMOJI_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    text = text.strip()
    if not text:
        return ""
    try:
        return sanitize_name(text)
    except ValueError:
        return ""


def fetch_nickname_from_profile(sec_uid: str) -> Optional[str]:
    """从用户主页在线爬取作者昵称（Playwright + Firefox 无头）。

    未登录无 cookie 打开主页——昵称是公开信息，2026-10-13 探针实测
    未登录无登录墙；轮询 DOM 直到 CSR 渲染出昵称：首选 h1（实测即纯
    昵称文本），回退 [data-e2e="user-info"] 首行。登录墙/改版/超时/
    浏览器异常等任何失败 → None（回退调用方默认），绝不抛出。
    """
    if not sec_uid:
        return None
    if _PROJECT_BROWSERS_DIR.is_dir() and any(_PROJECT_BROWSERS_DIR.glob("firefox-*")):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_PROJECT_BROWSERS_DIR))
    from playwright.sync_api import sync_playwright  # 延迟导入（离线测试友好）

    pw = None
    browser = None
    try:
        pw = sync_playwright().start()
        browser = pw.firefox.launch(headless=True)
        page = browser.new_page()
        page.goto(
            _PROFILE_URL.format(sec_uid=sec_uid),
            wait_until="domcontentloaded",
            timeout=_GOTO_TIMEOUT_MS,
        )
        deadline = time.time() + _DOM_POLL_SEC
        while time.time() < deadline:
            found = page.evaluate(
                """() => {
                    const txt = (el) => el ? (el.innerText || '').trim() : '';
                    const h1 = txt(document.querySelector('h1'));
                    if (h1) return h1;
                    const info = txt(document.querySelector('[data-e2e="user-info"]'));
                    if (info) return info.split('\\n')[0];
                    return '';
                }"""
            )
            found = (found or "").strip()
            if found and len(found) <= 100:  # 防改版后拿到大段文本
                return found
            page.wait_for_timeout(_DOM_POLL_INTERVAL_MS)
        return None
    except Exception:  # noqa: BLE001 - 探测通道任何失败静默回退
        return None
    finally:
        for closer in (
            lambda: browser.close(),
            lambda: pw.stop(),
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001 - 清理失败不影响结果
                pass


def fetch_nickname_by_aweme(aweme_id: str) -> Optional[str]:
    """单作品链接在线兜底：移动 Feed 免 cookie 通道拿作者昵称。

    与 f2 引擎 fetch_mobile_feed 同源（api.py MOBILE_FEED 主备节点，
    移动 UA 必带），f2dl 侧同步实现 + 短超时 fail-fast；仅 one 模式
    （单作品链接）使用。任何失败 → None（回退调用方默认）。
    """
    if not aweme_id:
        return None
    import httpx  # 延迟导入：仅真正联网时付出 import 成本（离线测试友好）

    from f2.apps.douyin.api import DouyinAPIEndpoints as dyendpoint

    for node in (dyendpoint.MOBILE_FEED, dyendpoint.MOBILE_FEED_BACKUP):
        try:
            resp = httpx.get(
                f"{node}?aweme_id={aweme_id}&aid=1128",
                headers={"User-Agent": dyendpoint.MOBILE_FEED_UA},
                follow_redirects=True,
                timeout=_ONLINE_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:  # noqa: BLE001 - 探测通道快速失败换节点
            continue
        for item in (data or {}).get("aweme_list") or []:
            if str(item.get("aweme_id", "")) == str(aweme_id):
                nickname = str(
                    ((item.get("author") or {}).get("nickname")) or ""
                ).strip()
                return nickname or None
        # 正常响应但未匹配（业务层软拒/作品不在推荐流）→ 不再试备用节点
        break
    return None


def resolve_nickname(platform_key: str, url: str) -> Tuple[str, str, str]:
    """链接 → (原始昵称, 清洗后昵称, 来源)；拿不到时 ("", "", "")。

    分发：sec_user_id（主页链接）→ 打开用户主页在线爬取（2026-10-13
    用户决策：不查本地缓存）；aweme_id（单作品链接）→ 移动 Feed 免
    cookie 在线探测；提取不到标识（分享短链等）→ 空三元组。
    来源恒为 "online"（保留字段供调用方提示使用）。
    """
    ident = extract_identifier(platform_key, url or "")
    if not ident:
        return "", "", ""
    if ident.startswith("MS4wLjAB"):  # sec_user_id → 用户主页 DOM 爬取
        raw = fetch_nickname_from_profile(ident)
    else:  # aweme_id（/video/{id}、/note/{id}）→ 移动 Feed 在线探测
        raw = fetch_nickname_by_aweme(ident)
    raw = (raw or "").strip()
    if not raw:
        return "", "", ""
    return raw, clean_nickname(raw), "online"