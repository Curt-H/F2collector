"""Cookie 的抓取（借助 f2 --auto-cookie）、手动粘贴与本地读写。

生命周期（Q2b/Q3b/Q4a/Q13a 共识）：cookie 永不持久化——
- 每个任务启动时先清空 cookie 存储（自愈强杀残留），再强制重抓：
  `f2 <alias> --auto-cookie <browser> -c <cookie 文件>`；
- 新 cookie 由编排层经 f2 原生 `--cookie` CLI 参数直传下载进程
  （merge_config 中 CLI 优先级最高、空值跳过——源码已核实），
  任务配置文件的 cookie 字段从生到死为空；
- 任务路径结束（无论成败/中断）把 cookie 存储写回空值。

cookie 文件结构与 f2 完全一致：{"douyin": {"cookie": "..."}}，
这样 f2 --auto-cookie -c <file> 可以直接写入我们约定的文件。
本模块的手动入口（dydl.py cookie / 向导菜单 3）仅用于诊断与救急。
"""

import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple

import yaml

from . import runner
from .apps import BROWSERS, TOOL_ROOT, get_platform


class CookieNotReadyError(RuntimeError):
    """cookie 获取失败（预检不过 / 抓取失败 / 结果为空）。

    批量追更（rerun-all）捕获此异常即中止整批（Q5②）；单任务交互场景
    在编排层就地回退（重试/手动粘贴，Q5①）。
    """


class BrowserRunningError(CookieNotReadyError):
    """所选浏览器进程仍在运行——其 cookie 库被锁定，抓取必然失败。"""


#: 浏览器 -> Windows 进程名映射（用于抓取前的预检，Q5③；冷门浏览器尽力覆盖）
_BROWSER_PROCESSES = {
    "chrome": ["chrome.exe"],
    "firefox": ["firefox.exe"],
    "edge": ["msedge.exe"],
    "opera": ["opera.exe"],
    "opera_gx": ["opera.exe", "opera_gx.exe"],
    "safari": ["safari.exe"],
    "chromium": ["chromium.exe", "chrome.exe"],
    "brave": ["brave.exe"],
    "vivaldi": ["vivaldi.exe"],
    "librewolf": ["librewolf.exe"],
}


def cookie_file(platform_key: str) -> Path:
    return get_platform(platform_key)["cookie_file"]


def load_cookie(platform_key: str) -> str:
    path = cookie_file(platform_key)
    if not path.exists():
        return ""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = data.get(get_platform(platform_key)["app"]) or {}
    return str(section.get("cookie") or "")


def save_cookie(platform_key: str, cookie: str) -> Path:
    plat = get_platform(platform_key)
    path = plat["cookie_file"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump({plat["app"]: {"cookie": cookie}}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def clear_cookie(platform_key: str) -> Path:
    """清空 cookie 存储：写回空值、保留文件结构（Q13a）。

    任务启动前调用是自愈（覆盖强杀残留的旧 cookie），任务结束后调用是
    生命周期约定（cookie 不落盘过夜，Q3b 无论成败都清）。
    """
    return save_cookie(platform_key, "")


def ensure_browser_closed(browser: str) -> None:
    """抓取前的浏览器进程预检（Q5③）：仍在运行则抛 BrowserRunningError。

    f2 --auto-cookie 需要读取浏览器的 cookie 库，浏览器不关必失败
    （Firefox 的 cookies.sqlite 被进程锁定），提前给出直白报错而不是让
    f2 吐晦涩的 PermissionError。预检自身失败（tasklist 不可用等）软跳过，
    交由 f2 端兜底。
    """
    if browser not in BROWSERS:
        raise ValueError(f"不支持的浏览器: {browser}（可选: {', '.join(BROWSERS)}）")
    names = _BROWSER_PROCESSES.get(browser) or []
    if not names:
        return  # 无进程映射时跳过预检，不阻塞任务
    try:
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        listing = (completed.stdout or "").lower()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return  # 预检自身失败不阻塞，f2 端仍会兜底报错
    running = [name for name in names if name in listing]
    if running:
        raise BrowserRunningError(
            f"检测到 {browser} 浏览器进程（{', '.join(running)}）正在运行，"
            f"无法抓取 cookie。请完全退出 {browser}（含托盘/后台进程）后重试。"
        )


def grab_cookie(platform_key: str, browser: str = "firefox") -> Tuple[bool, str]:
    """进程内直接用 browser_cookie3 抓取 cookie（本地补丁 O1，2026-09-16）。

    原实现起整个 f2 --auto-cookie 子进程（Python 冷启动 + import f2 全家桶
    + click.confirm 交互，每任务 4~7 秒）；核实 f2 端实现（f2/utils/utils.py
    的 get_cookie_from_browser + split_dict_cookie）后，改为本进程内等价
    直抓，格式与 f2 auto-cookie 产物完全一致：
    browser_cookie3.<browser>(domain_name=站点域) → "; ".join(name=value)。
    调用前请先 ensure_browser_closed 预检（cookie 库锁定问题依然存在）。
    """
    if browser not in BROWSERS:
        raise ValueError(f"不支持的浏览器: {browser}（可选: {', '.join(BROWSERS)}）")

    try:
        import browser_cookie3  # 延迟导入（软依赖）
    except ImportError as exc:  # pragma: no cover - requirements 已含此依赖
        raise CookieNotReadyError(
            f"缺少 browser-cookie3 依赖，无法从 {browser} 抓取 cookie：{exc}"
        ) from exc

    # 本地补丁(C3, 2026-09-17)：下方域名硬编码 douyin.com——未注册平台
    # 显式报错，防止未来解封微博等平台时静默抓错站点的 cookie。
    if platform_key != "douyin":
        raise NotImplementedError(
            "grab_cookie 目前仅支持 douyin（域名硬编码 douyin.com），不支持: "
            f"{platform_key}"
        )
    # 本地补丁(C4, 2026-09-17)：browser→抓取函数直接查库属性，替代原先
    # 10 分支手写字典（browser 已过 BROWSERS 校验，此处兜底防御）。
    fn = getattr(browser_cookie3, browser, None)
    if fn is None:
        raise CookieNotReadyError(f"browser_cookie3 不支持从 {browser} 抓取 cookie")
    domain = "douyin.com"

    try:
        jar = fn(domain_name=domain)
        header = "; ".join(f"{c.name}={c.value}" for c in jar)
    except CookieNotReadyError:
        raise
    except Exception as exc:  # noqa: BLE001 - 库锁定/解密失败等统一降级
        raise CookieNotReadyError(
            f"从 {browser} 读取 {domain} cookie 库失败：{exc}"
        ) from exc

    if not header:
        return False, ""
    save_cookie(platform_key, header)
    return True, header



def mask(cookie: str, head: int = 16, tail: int = 8) -> str:
    if not cookie:
        return "(空)"
    if len(cookie) <= head + tail:
        return cookie
    return f"{cookie[:head]}...{cookie[-tail:]}"


def paste_cookie(platform_key: str, raw: str) -> str:
    """清洗并保存手动粘贴的 cookie（Q9 回退路径），返回保存的值。"""
    cookie = (raw or "").strip().strip('"').strip("'")
    if len(cookie) < 20 or ";" not in cookie:
        raise ValueError("粘贴内容不像有效的 cookie（应为一串 key=value; key=value）")
    save_cookie(platform_key, cookie)
    return cookie
