# -*- coding: utf-8 -*-
"""诊断探针 v2（FOLLOW-1 排障）：dump 主页 user-info 统计区文本结构，
改用「容器内文本匹配 N 关注」点击，验证能否触发 following/list 接口。"""

import re
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
WATCH_SEC = 25


def main() -> int:
    cookie_mod.ensure_browser_closed("firefox")
    ok, cookie_header = cookie_mod.grab_cookie("douyin", "firefox")
    if not ok or not cookie_header:
        print("[ERR] cookie grab failed")
        return 1
    print("[OK] cookie grabbed")

    following_hits = []
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

            def on_response(resp):
                u = resp.url
                if "following" in u or "relation" in u:
                    print(f"[HIT ] {resp.status} {u[:170]}")
                    if "following" in u:
                        following_hits.append(resp)

            page.on("response", on_response)
            page.goto(
                USER_URL.format(sec_uid=SEC_UID),
                wait_until="domcontentloaded",
                timeout=90_000,
            )
            if _detect_login_wall(page):
                print("[DETECT] *** LOGIN WALL ***")

            info = page.locator('[data-e2e="user-info"]').first
            print(f"[UI-TEXT] {info.inner_text()!r}")

            stat = info.get_by_text(re.compile(r"\d[\d万.,]*\s*关注")).first
            print(f"[STAT-COUNT] {stat.count()}")
            stat.click(timeout=5000)
            print("[CLICKED] stat text")
            page.wait_for_timeout(WATCH_SEC * 1000)

            n_users = page.locator('a[href*="/user/MS4w"]').count()
            print(f"[MODAL] a[href*=/user/MS4w] count = {n_users}")
            page.screenshot(path=str(TOOL_ROOT / "logs" / "probe_following2.png"))
            print("[SHOT] logs/probe_following2.png")

            for i, resp in enumerate(following_hits[:2]):
                try:
                    body = resp.text()[:600].replace("\n", " ")
                except Exception:  # noqa: BLE001
                    body = "<body err>"
                print(f"[BODY{i}] {body}")
        finally:
            cookie_mod.clear_cookie("douyin")
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
