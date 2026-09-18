# -*- coding: utf-8 -*-
"""关注列表导出工具（FOLLOW-1）：导出当前登录账号全部关注用户的昵称与主页链接。

用法：
    python utils/following_list.py                       # 交互输入自己的主页链接
    python utils/following_list.py -u <我的主页链接>      # 参数直传
    python utils/following_list.py --paste               # 手动粘贴 cookie（不抓浏览器）
    python utils/following_list.py --headed              # 强制有头模式（排障观察）

输出：utils/output/following_YYYYMMDD.txt，每个关注用户两行——
    昵称一行
    https://www.douyin.com/user/<sec_uid> 一行

通道：Firefox 真实浏览器（Playwright 无头优先、失败自动有头重试），旁路拦截
关注列表接口响应——页面 JS 现场签名，Python 不参与构造请求，与作品列表
浏览器通道（f2/apps/douyin/browser_fetch.py）同思路；不设 httpx 兜底。
cookie 复用主工具生命周期：抓取（需关闭系统 Firefox）→ 使用 → 结束即清空。
"""

import argparse
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from f2.apps.douyin.utils import SecUserIdFetcher  # noqa: E402
from f2dl import cookie as cookie_mod  # noqa: E402
from f2dl.apps import extract_identifier  # noqa: E402

#: 关注列表接口 path（由页面 JS 现场签名发起，Python 旁路拦截）
FOLLOWING_PATH_MARK = "/aweme/v1/web/user/following/list/"

USER_URL = "https://www.douyin.com/user/{sec_uid}"

OUTPUT_DIR = Path(__file__).resolve().parent / "output"

#: 翻页滚动节奏（沿用作品列表通道实测节奏，不发明新值）
SCROLL_CD_RANGE_SEC = (5, 15)

#: 新页轮询间隔（秒）
POLL_INTERVAL_SEC = 0.5

#: 连续无新页的滚动轮数上限 → 收尾
NO_NEW_PAGE_ROUNDS = 3

#: 单次运行总预算（秒）——失控保险丝
TOTAL_BUDGET_SEC = 10 * 60

#: 弹层打开后等待首屏响应的超时（秒）
FIRST_PAGE_TIMEOUT_SEC = 45

#: 页面加载超时（毫秒，与作品列表通道一致）
GOTO_TIMEOUT_MS = 90_000

_SCROLL_BOTTOM_JS = """
() => {
    let best = null;
    for (const el of document.querySelectorAll('div')) {
        if (el.scrollHeight > el.clientHeight + 200 && el.clientHeight > 200) {
            if (!best || el.scrollHeight > best.scrollHeight) best = el;
        }
    }
    if (best) best.scrollTop = best.scrollHeight;
}
"""


def resolve_sec_uid(url: str) -> str:
    """主页链接 → sec_uid。完整 user 链接本地正则提取（免网络）；
    分享短链走引擎 SecUserIdFetcher 网络解析。失败返回空串。"""
    sec = extract_identifier("douyin", url or "")
    if sec and sec.startswith("MS4wLjAB"):
        return sec
    import asyncio

    try:
        return asyncio.run(SecUserIdFetcher.get_sec_user_id(url)) or ""
    except Exception:  # noqa: BLE001 - 网络解析失败交由上层报错
        return ""


def format_lines(items) -> str:
    """[(昵称, sec_uid)] → 每个用户两行（昵称一行、链接一行）的文本。"""
    out = []
    for nickname, sec_uid in items:
        out.append(str(nickname or "").strip())
        out.append(USER_URL.format(sec_uid=sec_uid))
    return "\n".join(out) + ("\n" if out else "")


def accumulate(pages):
    """跨页累计关注用户（按 sec_uid 去重、保持首见顺序）。

    pages: 每页拦截到的原始 JSON dict 列表（与 UserFollowingFilter 同构）。
    返回 (items, total, status_code)：items=[(昵称, sec_uid)]。
    """
    from f2.apps.douyin.filter import UserFollowingFilter

    items = []
    seen = set()
    total = 0
    status_first = None
    for raw in pages:
        f = UserFollowingFilter(raw)
        if status_first is None:
            status_first = f.status_code
        if f.total:
            total = f.total
        for nickname, sec_uid in zip(f.nickname, f.sec_uid):
            if not sec_uid or sec_uid in seen:
                continue
            seen.add(sec_uid)
            items.append((str(nickname or "").strip(), str(sec_uid)))
    return items, total, status_first


def _open_following_modal(page) -> bool:
    """多策略点开主页上的“关注”弹层。返回是否点击成功
    （是否真出数据由外层按拦截响应判定）。"""
    # 策略1：常见 data-e2e 命名
    for sel in (
        '[data-e2e="user-info-follow"]',
        '[data-e2e="following-list"]',
        '[data-e2e="user-following"]',
    ):
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible():
                el.click(timeout=3000)
                return True
        except Exception:  # noqa: BLE001 - 选择器未命中换下一个
            continue
    # 策略2：文本匹配“N 关注 / 关注 N”
    for pat in (r"^\d[\d万.,]*\s*关注\s*$", r"^关注\s*\d[\d万.,]*\s*$"):
        try:
            el = page.get_by_text(re.compile(pat)).first
            if el.count():
                el.click(timeout=3000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _scroll_once(page, posture: int) -> None:
    """轮换滚动姿势：滚轮 → End 键 → 容器 JS 滚底。"""
    try:
        if posture == 0:
            page.mouse.wheel(0, 2400)
        elif posture == 1:
            page.keyboard.press("End")
        else:
            page.evaluate(_SCROLL_BOTTOM_JS)
    except Exception:  # noqa: BLE001 - 单次滚动失败由“无新页”逻辑兜底
        pass


def collect(cookie_header: str, sec_uid: str, headless: bool = True):
    """打开自己的主页并旁路拦截关注列表响应，滚动累计到列表尽头。

    返回 (pages, stop_reason)：pages 为每页原始 JSON；stop_reason ∈
    {"has_more", "scroll_end", "budget"}。失败抛 RuntimeError（含可读原因）。
    """
    from playwright.sync_api import sync_playwright

    from f2.apps.douyin.browser_fetch import (
        _detect_login_wall,
        _parse_cookie_header,
    )

    pages = []
    stop_reason = ""
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=headless)
        try:
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080}, locale="zh-CN"
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined})"
            )
            cookies = _parse_cookie_header(cookie_header)
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()

            def on_response(resp):
                if FOLLOWING_PATH_MARK in resp.url:
                    try:
                        pages.append(resp.json())
                    except Exception:  # noqa: BLE001 - 单页解析失败不中断
                        pass

            page.on("response", on_response)
            page.goto(
                USER_URL.format(sec_uid=sec_uid),
                wait_until="domcontentloaded",
                timeout=GOTO_TIMEOUT_MS,
            )
            if _detect_login_wall(page):
                raise RuntimeError(
                    "检测到登录墙：cookie 无登录态（重新抓取或用 --paste 粘贴）"
                )
            if not _open_following_modal(page):
                raise RuntimeError(
                    '未能打开"关注"弹层（页面结构可能变化；加 --headed 观察）'
                )
            deadline = time.time() + FIRST_PAGE_TIMEOUT_SEC
            while time.time() < deadline and not pages:
                page.wait_for_timeout(int(POLL_INTERVAL_SEC * 1000))
            if not pages:
                raise RuntimeError(
                    "弹层已开但未拦截到关注列表响应（疑似接口变动或风控拦截；"
                    "加 --headed 观察）"
                )
            budget = time.time() + TOTAL_BUDGET_SEC
            fail_rounds = 0
            posture = 0
            while time.time() < budget:
                n_before = len(pages)
                _scroll_once(page, posture)
                posture = (posture + 1) % 3
                cd_end = time.time() + random.uniform(*SCROLL_CD_RANGE_SEC)
                while time.time() < cd_end:
                    page.wait_for_timeout(int(POLL_INTERVAL_SEC * 1000))
                if len(pages) == n_before:
                    fail_rounds += 1
                    if fail_rounds >= NO_NEW_PAGE_ROUNDS:
                        stop_reason = "scroll_end"
                        break
                else:
                    fail_rounds = 0
                if pages and pages[-1].get("has_more") is False:
                    stop_reason = "has_more"
                    break
            if not stop_reason:
                stop_reason = "budget"
        finally:
            browser.close()
    return pages, stop_reason


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="导出当前登录账号全部关注用户的昵称与主页链接"
    )
    ap.add_argument("-u", "--url", help="当前登录账号的主页链接（缺省交互输入）")
    ap.add_argument("--paste", action="store_true", help="手动粘贴 cookie（不抓浏览器）")
    ap.add_argument("--headed", action="store_true", help="强制有头模式（排障观察）")
    args = ap.parse_args(argv)

    url = (args.url or input("请粘贴当前登录账号的主页链接：")).strip()
    sec_uid = resolve_sec_uid(url)
    if not sec_uid:
        print("[ERR] 无法从链接解析 sec_uid（应为完整主页链接或分享短链）")
        return 1
    print(f"sec_uid = {sec_uid[:24]}...")

    if args.paste:
        raw = input("粘贴 cookie：").strip().strip('"').strip("'")
        if len(raw) < 20 or ";" not in raw:
            print("[ERR] 粘贴内容不像有效的 cookie")
            return 1
        cookie_header = cookie_mod.paste_cookie("douyin", raw)
    else:
        cookie_mod.ensure_browser_closed("firefox")
        ok, cookie_header = cookie_mod.grab_cookie("douyin", "firefox")
        if not ok or not cookie_header:
            print("[ERR] cookie 抓取失败（完全关闭 Firefox 后重试，或用 --paste）")
            return 1
    print(f"cookie: {cookie_mod.mask(cookie_header)}")

    try:
        headless = not args.headed
        try:
            pages, stop = collect(cookie_header, sec_uid, headless=headless)
        except Exception as exc:  # noqa: BLE001 - 无头失败自动有头重试一次
            if not headless:
                raise
            print(f"[WARN] 无头模式失败（{exc}），自动改用有头模式重试……")
            pages, stop = collect(cookie_header, sec_uid, headless=False)

        items, total, status = accumulate(pages)
        if status == 2096:
            print("[ERR] 目标账号的关注列表不可见（隐私设置）")
            return 1
        if not items:
            print("[ERR] 未收集到任何关注用户")
            return 1

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out = OUTPUT_DIR / f"following_{datetime.now():%Y%m%d}.txt"
        out.write_text(format_lines(items), encoding="utf-8")
        print(
            f"[OK] 已导出 {len(items)} 个关注用户"
            f"（服务端 total={total}，停止原因={stop}）→ {out}"
        )
        if total and len(items) < total:
            print(f"[WARN] 缺口 {total - len(items)} 条（预算耗尽或列表含异常条目）")
        return 0
    finally:
        cookie_mod.clear_cookie("douyin")


if __name__ == "__main__":
    sys.exit(main())
