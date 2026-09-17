# ruff: noqa
"""P0 浏览器通道探针（只读实验,不改动 F2DL/F2 任何代码）。

背景:2026-09-15 httpx 侧 /aweme/v1/web/aweme/post/ 在探针 v1 中 8/8 全 403
(Argus "Uifid Not Found"),UIFID header 与纯 Python a_bogus 签名均无法真正
通过设备凭证校验。

假设:真实浏览器里该接口由页面 JS 自行签名发起(msToken/a_bogus 在内存生成,
不依赖 Set-Cookie 颁发 msToken——与已删除的 msToken 保活实验(token_refresh.py)的"等 cookie
颁发"思路本质不同),Python 只需旁路拦截响应即可拿到作品列表。

通道(判定标准对应第 0 步计划):
  A_headless_first_page   无头:首屏 post 响应 200 且 aweme_list 有数据
  B_headless_scroll       无头:滚动触发第 2 页(max_cursor 前进)
  C_headed_first_page     有头:同 A(短暂弹窗,自动关闭)
  D_headed_scroll         有头:同 B
  E_diag                  失败诊断:截图 + 页面 URL/标题/关键词 → logs/

安全边界:只读——不改任务配置、不写 cookie 存储、不动 dy/conf;
cookie 仅驻内存,诊断截图落 logs/。请求量:每会话 1 次加载 + ≤3 次滚动。

用法:
  python probe_browser_post.py                  # 全部通道(无头→冷却→有头)
  python probe_browser_post.py --headless-only  # 仅 A/B
  python probe_browser_post.py --headed-only    # 仅 C/D
"""

import argparse
import sys
import time
import warnings
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import yaml

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))

warnings.filterwarnings("ignore", category=Warning)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

#: 与 probe_douyin_risk.py v1/v2 同款对照账号(softFuFu)
SEC_USER_ID = "MS4wLjABAAAAZgFvH5X9vW0JfoAZWxoRPMMwStbsk5hK3AplFyKTfIg"
POST_PATH_MARK = "/aweme/v1/web/aweme/post/"
USER_URL = f"https://www.douyin.com/user/{SEC_USER_ID}"

GOTO_TIMEOUT_MS = 30_000
FIRST_PAGE_TIMEOUT_SEC = 30
SCROLL_ROUNDS = 3
SCROLL_TIMEOUT_SEC = 12
COOL_DOWN_SEC = 5  # 无头/有头会话之间冷却

#: 打码展示的敏感参数(只留前 16 字符证明"页面自己签的名")
SENSITIVE_PARAMS = {"mstoken", "a_bogus", "_signature", "uifid", "webid"}

#: E_diag 登录墙/验证码特征关键词
DIAG_KEYWORDS = ["请登录", "扫码登录", "验证码", "拖动滑块", "captcha", "verify", "login"]


def load_cookie() -> str:
    """读取 dy/cookie.yaml 登录 cookie(与 probe_douyin_risk.py 同款)。"""
    path = TOOL_ROOT / "dy" / "cookie.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return str((data.get("douyin") or {}).get("cookie") or "")
    except Exception:  # noqa: BLE001
        return ""


def parse_cookie_header(header: str) -> list:
    """cookie header → playwright add_cookies 数组。"""
    cookies = []
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name and value:
            cookies.append(
                {"name": name, "value": value, "domain": ".douyin.com", "path": "/"}
            )
    return cookies


def mask_url(url: str) -> str:
    """打码敏感参数值,保留参数名与前 16 字符(证明签名由页面生成)。"""
    try:
        parts = urlsplit(url)
        q = parse_qsl(parts.query, keep_blank_values=True)
        masked = [
            (
                k,
                (v[:16] + "…") if k.lower() in SENSITIVE_PARAMS and len(v) > 16 else v,
            )
            for k, v in q
        ]
        return f"{parts.scheme}://{parts.netloc}{parts.path}?{urlencode(masked)}"
    except Exception:  # noqa: BLE001
        return url[:160]


def consume_response(resp) -> dict:
    """提取 post 响应摘要;body 解析失败仅记录错误不抛出。"""
    info = {"url": mask_url(resp.url), "status": resp.status, "ok_json": False}
    try:
        data = resp.json()
        info["ok_json"] = True
        info["aweme_count"] = len(data.get("aweme_list") or [])
        info["max_cursor"] = data.get("max_cursor")
        info["has_more"] = data.get("has_more")
        first = (data.get("aweme_list") or [None])[0]
        if isinstance(first, dict):
            info["first_id"] = str(first.get("aweme_id") or "")
            info["first_desc"] = str(first.get("desc") or "")[:24]
    except Exception as exc:  # noqa: BLE001
        info["body_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
    return info


def run_session(p, headless: bool, cookies: list, tag: str) -> dict:
    """一次浏览器会话:首屏(A/C) + 滚动翻页(B/D)。返回摘要 dict。"""
    result = {
        "mode": "headless" if headless else "headed",
        "first": None,
        "pages": [],
        "diag": None,
    }
    browser = p.firefox.launch(headless=headless)
    try:
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            # 不覆盖 User-Agent:让 playwright Firefox 使用自身真实指纹
        )
        # 基础反检测:抹掉 navigator.webdriver
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        if cookies:
            context.add_cookies(cookies)
        page = context.new_page()

        captured = []  # [info, ...]

        def on_response(resp):
            try:
                if POST_PATH_MARK in resp.url:
                    captured.append(consume_response(resp))
            except Exception:  # noqa: BLE001 - 监听器绝不抛出
                pass

        page.on("response", on_response)
        page.goto(USER_URL, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)

        # A/C:等待首屏 post 响应
        deadline = time.time() + FIRST_PAGE_TIMEOUT_SEC
        while time.time() < deadline and not captured:
            page.wait_for_timeout(500)
        if not captured:
            # E_diag:无 post 响应,落盘诊断
            shot = TOOL_ROOT / "logs" / f"probe_browser_{tag}.png"
            try:
                page.screenshot(path=str(shot), full_page=False)
            except Exception:  # noqa: BLE001
                shot = None
            content = ""
            try:
                content = page.content() or ""
            except Exception:  # noqa: BLE001
                pass
            hits = sorted({k for k in DIAG_KEYWORDS if k in content})
            result["diag"] = {
                "final_url": page.url,
                "title": page.title(),
                "keyword_hits": hits,
                "screenshot": str(shot) if shot else None,
            }
            return result

        result["first"] = captured[0]
        result["pages"].append(captured[0])

        # B/D:滚动触发翻页(最多 SCROLL_ROUNDS 轮,拿到第 2 页即停)
        first_cursor = captured[0].get("max_cursor")
        for _ in range(SCROLL_ROUNDS):
            try:
                page.mouse.wheel(0, 4000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(1500)
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:  # noqa: BLE001
                pass
            deadline = time.time() + SCROLL_TIMEOUT_SEC
            while time.time() < deadline:
                if any(
                    c.get("max_cursor") not in (None, first_cursor) for c in captured
                ):
                    break
                page.wait_for_timeout(500)
            if any(c.get("max_cursor") not in (None, first_cursor) for c in captured):
                break

        result["pages"].extend(captured[1:])
        context.close()
    finally:
        browser.close()
    return result


def print_result(name: str, r: dict) -> None:
    mode = r["mode"]
    first = r["first"]
    if first:
        print(
            f"[{name}] {mode} 首屏: HTTP {first['status']} "
            f"aweme_list={first.get('aweme_count', '?')}条 "
            f"max_cursor={first.get('max_cursor')} has_more={first.get('has_more')}"
        )
        if first.get("first_id"):
            print(f"    首条: {first['first_id']} | {first.get('first_desc', '')}")
        print(f"    请求URL样本: {first['url'][:170]}")
        if not first.get("ok_json"):
            print(f"    ⚠ body 解析失败: {first.get('body_error')}")
    else:
        print(f"[{name}] {mode} 首屏: ❌ 未捕获到 post 响应")
        d = r.get("diag") or {}
        if d:
            print(f"    final_url: {str(d.get('final_url', ''))[:120]}")
            print(f"    title: {str(d.get('title', ''))[:80]}")
            print(f"    关键词命中: {d.get('keyword_hits') or '无'}")
            if d.get("screenshot"):
                print(f"    截图: {d['screenshot']}")
        return

    # B/D:翻页判定(max_cursor 相对首屏前进即算触发)
    first_cursor = first.get("max_cursor")
    name2 = name.replace("first_page", "scroll")
    scroll_pages = [pg for pg in r["pages"][1:] if pg.get("max_cursor") != first_cursor]
    if scroll_pages:
        pg = scroll_pages[0]
        print(
            f"[{name2}] {mode} 翻页: ✅ 第2页 HTTP {pg['status']} "
            f"aweme_list={pg.get('aweme_count', '?')}条 "
            f"max_cursor={pg.get('max_cursor')} has_more={pg.get('has_more')}"
        )
    elif first.get("has_more") is False:
        print(f"[{name2}] {mode} 翻页: (首屏 has_more=False,作品不足一页,非风控)")
    else:
        print(f"[{name2}] {mode} 翻页: ❌ 滚动 {SCROLL_ROUNDS} 轮未触发第 2 页")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless-only", action="store_true", help="仅 A/B(无头)")
    ap.add_argument("--headed-only", action="store_true", help="仅 C/D(有头)")
    args = ap.parse_args()

    cookie_header = load_cookie()
    cookies = parse_cookie_header(cookie_header)
    print("=" * 70)
    print("P0 浏览器通道探针 | Playwright Firefox 拦截 /aweme/v1/web/aweme/post/")
    print(
        f"cookie: {'有效载荷 %d 项' % len(cookies) if cookies else '空(匿名,预期登录墙)'}"
    )
    print(f"目标: {USER_URL}")
    print("=" * 70)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        if not args.headed_only:
            r1 = run_session(p, headless=True, cookies=cookies, tag="headless")
            print_result("A_headless_first_page", r1)
            print("-" * 70)
            print(f"(会话间冷却 {COOL_DOWN_SEC}s)")
            time.sleep(COOL_DOWN_SEC)
        if not args.headless_only:
            r2 = run_session(p, headless=False, cookies=cookies, tag="headed")
            print_result("C_headed_first_page", r2)

    print("=" * 70)
    print("判定: 首屏 200 且 aweme_list>0 → 通道成立;翻页需 max_cursor 前进;")
    print("      未捕获响应 → 看 E_diag 截图区分 登录墙/验证码/接口改版。")


if __name__ == "__main__":
    main()
