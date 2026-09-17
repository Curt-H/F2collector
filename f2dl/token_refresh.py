"""msToken/cookie 浏览器保活刷新（防风控）。

⚠️【功能已冻结 · 2026-09-11】本模块暂未启用（core.py 未接线），代码保留
待后续开发。冻结原因：实测抖音当前不在"访问首页"路径向自动化浏览器颁发
msToken cookie（Edge/Firefox × 无头/有头 × 滚动共 6 种组合均未颁发），
且内核 webcast 原逻辑工作正常；cookie 生命周期已自动化（f2dl/core.refresh_cookie：
每任务启动时 f2 --auto-cookie 重抓、--cookie CLI 直传、任务结束即清空）。

复活方法（三步接线 + 一处依赖）：
  1) core.py: `from . import token_refresh`
  2) core.py run_task / rerun_task: `token_refresh.ensure_fresh_token(platform_key)`
  3) core.py _run_with_retry 403 分支: `token_refresh.refresh_now(platform_key, force=True)`
  4) requirements.txt: 取消 playwright 条目的注释，并 `playwright install firefox`

以下为原模块文档：
用 playwright 的 Firefox 访问抖音完成风控握手（Firefox 反自动化检测比
Chromium 系宽松，且不受新版 Edge/Chrome 默认配置禁止自动化调试的限制），
提取浏览器颁发的真 msToken 写入缓存（vendored f2 的 TokenManager.
gen_real_msToken 会优先读取），并把续期后的 cookie 回写到 f2dl 存储与该
平台全部任务配置。全程软失败：任何异常只告警、退回现有 token 逻辑，
绝不阻断下载任务。
"""

import json
import time
from pathlib import Path
from typing import Optional

from . import confgen, console
from . import cookie as cookie_mod
from .apps import TOOL_ROOT, get_platform

#: msToken 缓存文件（vendored f2 内核 TokenManager 优先读取此文件）
CACHE_FILE = TOOL_ROOT / "f2dl" / "_cache" / "mstoken.json"

#: 缓存有效期（秒）——与 f2 内核 _load_cached_mstoken 的 30 分钟保持一致
MSTOKEN_TTL_SEC = 30 * 60


def load_cache(max_age: float = MSTOKEN_TTL_SEC) -> Optional[dict]:
    """读取有效缓存；缺失/损坏/超龄返回 None。"""
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        token = str(data.get("token") or "")
        fetched_at = float(data.get("fetched_at") or 0)
        if token and time.time() - fetched_at <= max_age:
            return data
    except Exception:  # noqa: BLE001 - 缓存读取永远软失败
        pass
    return None


def save_cache(token: str) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps({"token": token, "fetched_at": time.time()}, ensure_ascii=False),
        encoding="utf-8",
    )


def _parse_cookie_header(header: str) -> dict:
    pairs = {}
    for part in (header or "").split(";"):
        if "=" in part:
            name, _, value = part.strip().partition("=")
            if name:
                pairs[name] = value
    return pairs


def _refresh_with_playwright(cookie_header: str, headless: bool = True) -> Optional[dict]:
    """用 playwright 的 Firefox 访问抖音，返回 {"msToken", "cookie"}；失败返回 None。

    headless=True 为首选；无头指纹被识别（不颁发 msToken）时由调用方
    以 headless=False 重试一次。
    """
    from playwright.sync_api import sync_playwright  # 延迟导入（软依赖）

    # 剔除旧 msToken/ttwid：只带登录态进去，让浏览器现场重新颁发新 token
    keepalive = {
        name: value
        for name, value in _parse_cookie_header(cookie_header).items()
        if name not in ("msToken", "ttwid")
    }
    cookies = [
        {"name": name, "value": value, "domain": ".douyin.com", "path": "/"}
        for name, value in keepalive.items()
    ]
    with sync_playwright() as p:
        # playwright 自带的 Firefox build（无需系统安装 Firefox，也无需 channel）
        browser = p.firefox.launch(headless=headless)
        try:
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                # 不覆盖 User-Agent：让 playwright 的 Firefox 使用自身真实指纹
            )
            # 基础反检测：抹掉 navigator.webdriver 自动化标记
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined});"
            )
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
            if not headless:
                try:
                    page.bring_to_front()
                except Exception:  # noqa: BLE001 - 置前失败不影响流程
                    pass
            # 注意：douyin 对无头浏览器会拖延 domcontentloaded（反自动化手段），
            # 因此只等导航提交(commit)，随后轮询 cookie 等 JS 风控握手完成。
            page.goto(
                "https://www.douyin.com/",
                wait_until="commit",
                timeout=60_000,
            )
            # 轮询等待前端风控握手颁发新 msToken（最多约 30 秒）。
            # msToken 通常由风控 SDK 在发起 API 请求时落地——配合模拟滚动
            # 触发首页 feed 的 XHR，模拟真实用户行为。
            fresh = []
            deadline = time.time() + 30
            while time.time() < deadline:
                fresh = context.cookies("https://www.douyin.com/")
                if any(c.get("name") == "msToken" for c in fresh):
                    break
                try:
                    page.mouse.wheel(0, 1200)
                except Exception:  # noqa: BLE001 - 滚动失败不影响轮询
                    pass
                page.wait_for_timeout(1500)
        finally:
            browser.close()

    ms_token = next(
        (c["value"] for c in fresh if c.get("name") == "msToken"), ""
    )
    if not ms_token:
        return None
    header = "; ".join(f"{c['name']}={c['value']}" for c in fresh)
    return {"msToken": ms_token, "cookie": header}


def _write_back(platform_key: str, cookie: str) -> int:
    """cookie 回写：f2dl 存储 + 该平台全部任务配置，返回更新的配置数。"""
    from rich.markup import escape

    cookie_mod.save_cookie(platform_key, cookie)
    plat = get_platform(platform_key)
    updated = 0
    for conf_path in sorted(plat["conf_dir"].glob("*.yaml")):
        try:
            data = confgen.load_conf(conf_path) or {}
            section = data.get(plat["app"]) or {}
            if section.get("cookie"):
                section["cookie"] = cookie
                confgen.save_conf(conf_path, data)
                updated += 1
        except Exception:  # noqa: BLE001 - 单个配置回写失败不影响其余
            console.print(f"[dim]cookie 回写跳过: {escape(str(conf_path))}[/dim]")
            continue
    return updated


def refresh_now(platform_key: str, force: bool = False) -> bool:
    """立即刷新 msToken+cookie（全程软失败）。返回是否成功。

    仅支持抖音（msToken 是抖音风控要素）；force=True 时忽略缓存超龄检查。
    """
    if platform_key != "douyin":
        return False
    try:
        if not force and load_cache():
            return True
        cookie = cookie_mod.load_cookie(platform_key)
        if not cookie:
            console.print(
                "[yellow]没有可用 cookie，跳过 msToken 刷新（请先配置 cookie）。[/yellow]"
            )
            return False
        console.print("[dim]用 playwright Firefox 刷新 msToken/cookie（先无头，必要时弹窗重试）……[/dim]")
        result = _refresh_with_playwright(cookie, headless=True)
        if not result:
            # 共识 Q7 的升级路径：无头指纹被识别时用有头模式重试一次
            console.print(
                "[dim]无头模式未颁发 msToken，改用有头模式重试（会短暂弹出 Firefox 窗口）……[/dim]"
            )
            result = _refresh_with_playwright(cookie, headless=False)
        if not result:
            console.print(
                "[yellow]msToken 刷新未成功（浏览器未颁发 token），"
                "继续使用现有逻辑。[/yellow]"
            )
            return False
        save_cache(result["msToken"])
        updated = _write_back(platform_key, result["cookie"])
        console.print(
            f"[green]msToken/cookie 已刷新并回写 {updated} 个任务配置"
            f"（缓存 {MSTOKEN_TTL_SEC // 60} 分钟内复用）。[/green]"
        )
        return True
    except ImportError:
        console.print(
            "[yellow]未安装 playwright（pip install playwright），跳过刷新。[/yellow]"
        )
        return False
    except Exception as exc:  # noqa: BLE001 - 刷新永远软失败，不阻断任务
        from rich.markup import escape

        console.print(
            f"[yellow]msToken 刷新失败（不影响任务继续）：{escape(str(exc))}[/yellow]"
        )
        return False


def ensure_fresh_token(platform_key: str) -> bool:
    """任务启动前调用：缓存超龄/缺失时刷新，新鲜则直接复用（软失败）。"""
    if platform_key != "douyin":
        return False
    if load_cache():
        return True
    return refresh_now(platform_key, force=True)
