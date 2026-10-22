# -*- coding: utf-8 -*-
"""诊断探针 v5（FOLLOW-1 排障）：水合后出现的 trust-logout-dialog 信任验证框
是什么内容、能否关闭（Esc/关闭键），关闭后点击关注块能否拉出 following 接口。"""

import sys
import time
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
WATCH_SEC = 35


def dump_dialog(page, tag: str) -> None:
    try:
        dlg = page.locator("#trust-logout-dialog")
        if dlg.count():
            print(f"[DLG-{tag}] innerText = {dlg.first.inner_text()[:400]!r}")
            btns = dlg.locator("button, [role=button]").all_inner_texts()
            print(f"[DLG-{tag}] buttons = {btns}")
            print(f"[DLG-{tag}] html[:500] = {dlg.first.evaluate('e => e.outerHTML')[:500]}")
        else:
            print(f"[DLG-{tag}] absent")
    except Exception as exc:  # noqa: BLE001
        print(f"[DLG-{tag}] err {exc}")


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

            try:
                page.wait_for_function(
                    """() => {
                        const el = document.querySelector('[data-e2e="user-info-follow"]');
                        return el && /\\d/.test(el.innerText);
                    }""",
                    timeout=30_000,
                )
                print("[HYDRATED]")
            except Exception as exc:  # noqa: BLE001
                print(f"[HYDRATE-FAIL] {exc}")

            page.wait_for_timeout(5000)
            dump_dialog(page, "INITIAL")

            # 尝试1：Esc
            page.keyboard.press("Escape")
            page.wait_for_timeout(1500)
            dump_dialog(page, "AFTER-ESC")

            # 尝试2：点遮罩自身
            try:
                mask = page.locator(".trust-login-dialog-mask").first
                if mask.count():
                    mask.click(timeout=2000, force=True)
                    page.wait_for_timeout(1500)
            except Exception:  # noqa: BLE001
                pass
            dump_dialog(page, "AFTER-MASK")

            # 尝试3：点可能的关闭/取消按钮
            try:
                for txt in ("取消", "关闭", "暂不", "×"):
                    btn = page.locator("#trust-logout-dialog").get_by_text(
                        txt, exact=True
                    )
                    if btn.count():
                        btn.first.click(timeout=2000)
                        page.wait_for_timeout(1500)
                        break
            except Exception:  # noqa: BLE001
                pass
            dump_dialog(page, "AFTER-BTN")

            # 最终尝试点击关注块（force 以防仍有残余遮罩）
            try:
                page.locator('[data-e2e="user-info-follow"]').first.click(
                    timeout=5000, force=True
                )
                print("[CLICKED] stat block (force)")
            except Exception as exc:  # noqa: BLE001
                print(f"[CLICK-FAIL] {exc}")
            page.wait_for_timeout(WATCH_SEC * 1000)

            n_users = page.locator('a[href*="/user/MS4w"]').count()
            print(f"[MODAL] a[href*=/user/MS4w] count = {n_users}")
            page.screenshot(path=str(TOOL_ROOT / "logs" / "probe_following5.png"))
            print("[SHOT] logs/probe_following5.png")

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
