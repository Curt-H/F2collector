# -*- coding: utf-8 -*-
"""诊断探针 v4（FOLLOW-1 排障）：等待统计数字水合完成后再点击关注块，
点击后短窗口全量 dump 响应 URL，确认弹层数据通道与触发时机。"""

import sys
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(TOOL_ROOT), str(TOOL_ROOT / "utils")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pw_runtime import use_project_browsers  # noqa: E402

use_project_browsers()

from playwright.sync_api import sync_playwright  # noqa: E402

from f2.apps.douyin.browser_fetch import (  # noqa: E402
    _detect_login_wall,
    _parse_cookie_header,
)
from f2dl import cookie as cookie_mod  # noqa: E402

SEC_UID = "MS4wLjABAAAAyHcalN-UaAkKSz9c_yOSYOZne_kfqnTSjDRUo2yn-Lg_18K-v7l41ljnCjkVEmBV"
USER_URL = "https://www.douyin.com/user/{sec_uid}"
WATCH_SEC = 40
BURST_SEC = 8  # 点击后全量记录窗口


def main() -> int:
    cookie_mod.ensure_browser_closed("firefox")
    ok, cookie_header = cookie_mod.grab_cookie("douyin", "firefox")
    if not ok or not cookie_header:
        print("[ERR] cookie grab failed")
        return 1
    print("[OK] cookie grabbed")

    following_hits = []
    burst = []  # (秒偏移, url) 点击后短窗口内全部响应
    import time as _t

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        try:
            ctx = browser.new_context(
                viewport={"width": 1920, "height": 1080}, locale="zh-CN"
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined})"
            )
            cookies = _parse_cookie_header(cookie_header)
            if cookies:
                ctx.add_cookies(cookies)
            page = ctx.new_page()

            clicked_at = [None]

            def on_response(resp):
                u = resp.url
                if clicked_at[0] is not None and _t.time() - clicked_at[0] <= BURST_SEC:
                    burst.append(f"{_t.time() - clicked_at[0]:.1f}s {resp.status} {u[:150]}")
                if "following" in u:
                    print(f"[HIT ] {resp.status} {u[:170]}")
                    following_hits.append(resp)

            page.on("response", on_response)
            page.goto(
                USER_URL.format(sec_uid=SEC_UID),
                wait_until="domcontentloaded",
                timeout=90_000,
            )
            if _detect_login_wall(page):
                print("[DETECT] *** LOGIN WALL ***")

            # 等统计数字水合：user-info-follow 内出现数字
            try:
                page.wait_for_function(
                    """() => {
                        const el = document.querySelector('[data-e2e="user-info-follow"]');
                        return el && /\\d/.test(el.innerText);
                    }""",
                    timeout=30_000,
                )
                stat_text = page.locator(
                    '[data-e2e="user-info-follow"]'
                ).first.inner_text()
                print(f"[HYDRATED] stat text = {stat_text!r}")
            except Exception as exc:  # noqa: BLE001
                print(f"[HYDRATE-FAIL] {exc}")

            print(f"[URL-BEFORE] {page.url[:120]}")
            block = page.locator('[data-e2e="user-info-follow"]').first
            block.click(timeout=5000)
            clicked_at[0] = _t.time()
            print("[CLICKED] hydrated stat block")
            page.wait_for_timeout(WATCH_SEC * 1000)
            print(f"[URL-AFTER] {page.url[:120]}")

            print(f"[BURST] {len(burst)} responses in first {BURST_SEC}s:")
            for line in burst:
                print(f"  {line}")

            n_users = page.locator('a[href*="/user/MS4w"]').count()
            print(f"[MODAL] a[href*=/user/MS4w] count = {n_users}")
            page.screenshot(path=str(TOOL_ROOT / "logs" / "probe_following4.png"))
            print("[SHOT] logs/probe_following4.png")

            for i, resp in enumerate(following_hits[:2]):
                try:
                    body = resp.text()[:500].replace("\n", " ")
                except Exception:  # noqa: BLE001
                    body = "<body err>"
                print(f"[BODY{i}] {body}")
        finally:
            cookie_mod.clear_cookie("douyin")
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
