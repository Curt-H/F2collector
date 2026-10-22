# -*- coding: utf-8 -*-
"""诊断探针（FOLLOW-1 排障）：打开我的抖音主页，尝试点开关注弹层，
记录所有相关网络请求/响应 URL、页面 data-e2e 属性、弹层内用户条目数，
并截图到 logs/probe_following.png。只读诊断，不修改主工具。"""

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
WATCH_SEC = 40
KEYS = ("aweme", "follow", "user", "list")


def main() -> int:
    cookie_mod.ensure_browser_closed("firefox")
    ok, cookie_header = cookie_mod.grab_cookie("douyin", "firefox")
    if not ok or not cookie_header:
        print("[ERR] cookie grab failed")
        return 1
    print("[OK] cookie grabbed")

    json_hits = []
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

            def on_request(req):
                u = req.url
                if any(k in u for k in KEYS):
                    print(f"[REQ ] {req.method} {u[:170]}")

            def on_response(resp):
                u = resp.url
                if any(k in u for k in KEYS):
                    ct = resp.headers.get("content-type", "")
                    print(f"[RESP] {resp.status} {ct[:40]} {u[:170]}")
                    if "json" in ct:
                        json_hits.append(resp)

            page.on("request", on_request)
            page.on("response", on_response)
            page.goto(
                USER_URL.format(sec_uid=SEC_UID),
                wait_until="domcontentloaded",
                timeout=90_000,
            )
            print(f"[PAGE] title={page.title()!r}")
            print(f"[PAGE] url={page.url[:130]}")
            if _detect_login_wall(page):
                print("[DETECT] *** LOGIN WALL DETECTED ***")

            e2e = page.eval_on_selector_all(
                "[data-e2e]", "els => els.map(e => e.getAttribute('data-e2e'))"
            )
            print(f"[E2E ] {sorted(set(e2e))}")

            clicked = None
            for sel in (
                '[data-e2e="user-info-follow"]',
                '[data-e2e="following-list"]',
                '[data-e2e="user-following"]',
            ):
                try:
                    el = page.locator(sel).first
                    if el.count() and el.is_visible():
                        el.click(timeout=3000)
                        clicked = sel
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not clicked:
                for pat in (r"^\d[\d万.,]*\s*关注\s*$", r"^关注\s*\d[\d万.,]*\s*$"):
                    try:
                        el = page.get_by_text(re.compile(pat)).first
                        if el.count():
                            el.click(timeout=3000)
                            clicked = f"text:{pat}"
                            break
                    except Exception:  # noqa: BLE001
                        continue
            print(f"[CLICK] {clicked!r}")

            page.wait_for_timeout(WATCH_SEC * 1000)
            n_users = page.locator('a[href*="/user/MS4w"]').count()
            print(f"[MODAL] a[href*=/user/MS4w] count = {n_users}")
            shot = TOOL_ROOT / "logs" / "probe_following.png"
            page.screenshot(path=str(shot))
            print(f"[SHOT] {shot}")

            for i, resp in enumerate(json_hits[:6]):
                try:
                    body = resp.text()[:280].replace("\n", " ")
                except Exception:  # noqa: BLE001
                    body = "<body err>"
                print(f"[BODY{i}] {resp.url[:120]} => {body}")
        finally:
            cookie_mod.clear_cookie("douyin")
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
