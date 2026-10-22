# -*- coding: utf-8 -*-
"""Playwright 浏览器内核定位（便携化）。

背景：作品列表的 Firefox 真实浏览器通道（f2/apps/douyin/browser_fetch.py）
依赖 Playwright 内核。Playwright 默认把内核装在 %LOCALAPPDATA%\ms-playwright
（C 盘用户目录），不随项目目录走——换电脑后内核缺失，浏览器通道启动即报
"Executable doesn't exist"。

use_project_browsers()：项目内置内核目录 <仓库根>\ms-playwright\ 存在且含
firefox-* 内核时，在 playwright 驱动进程启动前设置 PLAYWRIGHT_BROWSERS_PATH
指向它（os.environ.setdefault，尊重外部显式配置），使整个项目目录拷到任何
电脑都能直接用；目录缺失时静默返回 False，回退系统默认内核位置，零破坏。

安装/重装内核：双击仓库根 install_browser.bat，或手动
    $env:PLAYWRIGHT_BROWSERS_PATH = "<仓库根>\ms-playwright"
    python\python.exe -m playwright install firefox
"""

import os
from pathlib import Path

#: 仓库根（本文件位于 utils/ 下，上跳一级）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
#: 项目内置 Playwright 内核目录
PROJECT_BROWSERS_DIR = PROJECT_ROOT / "ms-playwright"


def use_project_browsers() -> bool:
    """让 playwright 优先使用项目内置内核（存在时），返回是否已启用。

    必须在 sync_playwright()/async_playwright() 启动之前调用——内核
    路径在驱动进程启动时读取，之后设置无效。重复调用安全（setdefault）。
    """
    if not PROJECT_BROWSERS_DIR.is_dir():
        return False
    # 按 firefox-* 通配而非写死版本号（firefox-1538 等）——升级 playwright
    # 换内核 revision 时无需改这里
    if not any(PROJECT_BROWSERS_DIR.glob("firefox-*")):
        return False
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(PROJECT_BROWSERS_DIR))
    return True
