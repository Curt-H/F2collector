# ruff: noqa
"""探针补充验证:浏览器捕获的 post 响应 JSON 能否直接喂 f2 UserPostFilter。

即验证第 1 步的数据接缝:浏览器通道若产出与 fetch_user_post 同构的 dict,
下游 UserPostFilter(过滤/下载/DB)零改动。
只读实验:不改任何现有代码;响应样本落 logs/。
"""

import json
import sys
import time
import warnings
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]  # scripts/ 下上跳一级到仓库根
sys.path.insert(0, str(TOOL_ROOT))

from utils.pw_runtime import use_project_browsers  # noqa: E402

use_project_browsers()  # 项目内置 ms-playwright\ 内核存在时优先使用（便携化）

warnings.filterwarnings("ignore", category=Warning)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from probe_browser_post import (  # noqa: E402
    GOTO_TIMEOUT_MS,
    POST_PATH_MARK,
    USER_URL,
    parse_cookie_header,
)

SAMPLE_FILE = TOOL_ROOT / "logs" / "probe_browser_post_sample.json"


def main():
    from playwright.sync_api import sync_playwright

    from probe_browser_post import load_cookie  # noqa: E402
    from f2.apps.douyin.filter import UserPostFilter  # noqa: E402

    cookies = parse_cookie_header(load_cookie())
    payload = None
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080}, locale="zh-CN"
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
            holder = {}

            def on_response(resp):
                if POST_PATH_MARK in resp.url and "raw" not in holder:
                    try:
                        holder["raw"] = resp.json()
                    except Exception:  # noqa: BLE001
                        pass

            page.on("response", on_response)
            page.goto(USER_URL, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            deadline = time.time() + 30
            while time.time() < deadline and "raw" not in holder:
                page.wait_for_timeout(500)
            context.close()
        finally:
            browser.close()

    payload = holder.get("raw")
    if not payload:
        print("❌ 未捕获到 post 响应,接缝验证无法进行")
        return 1

    SAMPLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SAMPLE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    print(f"原始响应已落盘: {SAMPLE_FILE} ({SAMPLE_FILE.stat().st_size} bytes)")

    # 核心验证:与 fetch_user_post 返回值同构 → UserPostFilter 正常工作
    f = UserPostFilter(payload)
    print("--- UserPostFilter 解析成功 ---")
    print(f"  aweme_id : {f.aweme_id}")
    print(f"  desc     : {str(f.desc)[:40]}")
    print(f"  nickname : {f.nickname}")
    print(f"  create   : {f.create_time}")
    print(f"  has_more : {f.has_more} | max_cursor: {f.max_cursor}")
    raws = f._to_list()
    print(f"  _to_list : {len(raws)} 条作品")
    video_play = (raws[0] or {}).get("video_play_addr") if raws else None
    print(f"  首条视频直链存在: {bool(video_play)}")
    print("=== 接缝验证通过:浏览器 JSON → UserPostFilter 零改动 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
