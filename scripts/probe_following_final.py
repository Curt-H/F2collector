# -*- coding: utf-8 -*-
"""诊断探针 v6（FOLLOW-1 排障终审）：水合后统计块结构（是否变链接）、
正确点取消关闭信任框、点数字/标签区，全量记录点击后 12s 响应与所有 dialog。"""

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
    _parse_cookie_header,
)
from f2dl import cookie as cookie_mod  # noqa: E402

SEC_UID = "MS4wLjABAAAAyHcalN-UaAkKSz9c_yOSYOZne_kfqnTSjDRUo2yn-Lg_18K-v7l41ljnCjkVEmBV"
USER_URL = "https://www.douyin.com/user/{sec_uid}"
BURST_SEC = 12


def dump_state(page, tag: str) -> None:
    try:
        n_users = page.locator('a[href*="/user/MS4w"]').count()
        print(f"[{tag}] url={page.url[:110]}")
        print(f"[{tag}] a[href*=/user/MS4w] count = {n_users}")
        dlgs = page.locator('[role="dialog"], .semi-modal, [id*="dialog"]').all()
        texts = []
        for d in dlgs[:5]:
            try:
                t = d.inner_text()[:120].replace("\n", "|")
                if t.strip():
                    texts.append(t)
            except Exception:  # noqa: BLE001
                pass
        print(f"[{tag}] dialogs = {texts}")
    except Exception as exc:  # noqa: BLE001
        print(f"[{tag}] err {exc}")


# --- PART2 ---
def main() -> int:
    cookie_mod.ensure_browser_closed("firefox")
    ok, cookie_header = cookie_mod.grab_cookie("douyin", "firefox")
    if not ok or not cookie_header:
        print("[ERR] cookie grab failed")
        return 1
    print("[OK] cookie grabbed")

    following_hits = []
    burst = []
    clicked_at = [None]

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
                if clicked_at[0] is not None and time.time() - clicked_at[0] <= BURST_SEC:
                    burst.append(f"{time.time() - clicked_at[0]:.1f}s {resp.status} {u[:150]}")
                if "following" in u:
                    print(f"[HIT ] {resp.status} {u[:170]}")
                    following_hits.append(resp)

            page.on("response", on_response)
            page.goto(USER_URL.format(sec_uid=SEC_UID),
                      wait_until="domcontentloaded", timeout=90_000)

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

            blk = page.locator('[data-e2e="user-info-follow"]').first
            print(f"[BLK-HTML] {blk.evaluate('e => e.outerHTML')[:600]}")

            try:
                cancel = page.locator(
                    "#trust-logout-dialog .trust-login-dialog-button-cancel")
                if cancel.count():
                    cancel.first.click(timeout=3000)
                    page.wait_for_timeout(2000)
                    print("[DLG] cancel clicked")
                else:
                    print("[DLG] not present")
            except Exception as exc:  # noqa: BLE001
                print(f"[DLG] err {exc}")

            try:
                num = blk.locator("div").filter(has_text=r"^\d").first
                clicked_at[0] = time.time()
                burst.clear()
                num.click(timeout=4000)
                print("[CLICK-NUM] ok")
            except Exception as exc:  # noqa: BLE001
                print(f"[CLICK-NUM] fail {str(exc)[:120]}")
            page.wait_for_timeout(8000)
            print(f"[BURST-NUM] {len(burst)}:")
            for line in burst[:15]:
                print(f"  {line}")
            dump_state(page, "AFTER-NUM")

            if not following_hits:
                try:
                    label = blk.get_by_text("关注", exact=True).first
                    clicked_at[0] = time.time()
                    burst.clear()
                    label.click(timeout=4000)
                    print("[CLICK-LABEL] ok")
                except Exception as exc:  # noqa: BLE001
                    print(f"[CLICK-LABEL] fail {str(exc)[:120]}")
                page.wait_for_timeout(8000)
                print(f"[BURST-LABEL] {len(burst)}:")
                for line in burst[:15]:
                    print(f"  {line}")
                dump_state(page, "AFTER-LABEL")

            page.screenshot(path=str(TOOL_ROOT / "logs" / "probe_following6.png"))
            print("[SHOT] logs/probe_following6.png")

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

